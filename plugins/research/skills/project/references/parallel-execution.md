# Parallel task execution

> **Status: design awaiting implementation.** Nothing in this repository implements this protocol.
> No script, schema field, MCP server, or `SKILL.md` step exists for it, and this document is not
> linked from `SKILL.md` precisely so that no coordinator is told to follow a protocol the tooling
> cannot execute. It is a specification for a successor project to build against. Until that project
> ships, `research:project` executes tasks sequentially and delegation is out of policy.
>
> **Revision 4, 2026-09-11.** This document is the normative reference: current rules, schemas,
> operations and failure behaviour. It is self-contained, because `docs/` is not part of the installed
> plugin subtree — nothing an executing coordinator needs is outside this file. The revision history,
> the disposition of all three expert reviews, and the rejected and deferred alternatives are at
> [`docs/parallel-execution-decisions.md`](../../../../../docs/parallel-execution-decisions.md), a
> repository-only companion. Read that document to learn why a rule is what it is; read this one to
> implement it.
>
> Its structural claims are checked by `tests/plugins/research/test_parallel_execution_doc.py`, which
> re-reads every cited source range rather than trusting §22, resolves every link and section
> reference, and compares this document's canonical enums and transitions against
> `workspace_lib.py`. Its protocol invariants are checked by
> `tests/plugins/research/test_parallel_execution_model.py`, a design-stage executable model. Neither
> is an implementation, and neither establishes that the protocol is correct — §20 says exactly what
> each one does and does not settle.

## 1. Scope and objective

A protocol for executing the tasks of one `research:project` plan **concurrently**: independent tasks
dispatched by one coordinator to Claude Code subagents, with results published through a durable
journal and integrated into canonical state by that same coordinator, which remains the sole writer
of it.

It is a protocol, not an engine. The machinery it needs mostly exists already; §3 says exactly what.

The objective is **lower completion time and lower coordinator context consumption, at preserved
quality and controlled total cost.** Two things follow that are easy to get wrong:

- **Wider plans are a technique, not the goal.** §2 shows this workspace's plans are shaped like
  chains, so guidance on authoring is part of the deliverable (§16) — but a plan fragmented to raise
  average level width trades one coherent artifact for a metric, and increases assembly and review
  work while the number goes up.
- **Context economy is a hypothesis with a benchmark attached, not an established benefit.** On a host
  that provides bounded worker context (§17), a worker's tool output does not enter the coordinator's
  context window, and on a long project that is plausibly the larger win. On a host that does not, the
  benefit is unavailable and the report must say so. Nobody has measured either case. §20 says what
  would settle it, and until that runs, this document may not sell it as fact.

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
committed by Stage 2 (§20.3); until then, the figures are reproducible in principle and
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
  across files**, nor **refusal to overwrite** — `os.replace` clobbers by definition — which is why
  §7.1 and §7.2 exist.
- **Canonical state already carries the enums this protocol keys on.** Authorization statuses are
  `not_required`, `pending`, `explicit`, `denied`, `deferred` [C17], with `required` and `status`
  cross-checked [C18] and an explicit-authorization rule that fires only for `RUNNING` and `DONE`
  tasks [C19]; task transitions are a fixed map [C20]. §8.1 and §14.1 are written against those
  values rather than against invented ones.

So six things are missing, and they are what this document supplies: a **scheduler**, a **worker
contract**, a **publication and acceptance protocol**, a **recovery contract**, a **resource model**,
and a **host-adapter contract**.

## 4. Architecture

```
                 ┌──────────────────────────────────────────────┐
   canonical ────│  project.json   (revision-checked commit)     │  coordinator writes only
                 │  execution: {generation, attempts}  (§5.5)    │  the fence for §12
                 └──────────────────────────────────────────────┘
                                    ▲ integrates accepted results
                                    │
                 ┌──────────────────────────────────────────────┐
   durable   ────│  <project-dir>/execution/journal/            │  append-only, publish-if-absent
                 │  attempts/<id>/<record>.json    (§5.1)       │  coordinator + assigned writer
                 └──────────────────────────────────────────────┘
                                    ▲ claims
                 ┌──────────────────────────────────────────────┐
   workspace ────│  <workspace-root>/.execution-registry/       │  every executor, inline included
                 │  registry.lock/ · grants/<id>.json   (§8.5)  │
                 └──────────────────────────────────────────────┘
                     ▲ dispatch (down)      ▲ result (up, published per §7.2)
                     │                      │
              ┌──────┴─────┐  ┌────────────┐  ┌────────────┐
              │  worker 1  │  │  worker 2  │  │  worker N  │   N ≤ 4 (§8.4)
              └────────────┘  └────────────┘  └────────────┘
                 filesystem mode chosen per §9, not assumed shared
```

Four properties define it:

1. **The coordinator assigns; workers do not claim.** At four workers the coordinator already owns
   every decision before execution (eligibility, authorization, resources, capacity) and after it
   (verification, evidence, commit). A worker discovering its own next task adds a distributed
   protocol to a problem that has none.
2. **The journal is the authority; every other view is a projection.** An attempt's label, the set of
   claims it holds, and whether dispatch may proceed are all *computed* from the set of journal
   records present (§5.2, §6.1). Nothing reads a mutable status field to make a decision, so there is
   no pair of representations that a crash can leave disagreeing.
3. **The middle layer is durable, not derived.** Its justification is *recovery*: a coordinator that
   dies holding an uncommitted result must not lose completed work. That means the journal's contents
   are classified (§5.6), retained under a stated policy (§5.7), and published in a defined order with
   a no-clobber primitive (§7.1).
4. **There is one scheduler.** Sequential execution is this scheduler at capacity one — not a second
   mechanism, and not an exemption from the registry (§8.5). A separate wave-synchronous path would be
   the code that runs on every degraded host and is therefore exercised least, which is how a fallback
   rots.

## 5. The execution store

### 5.1 Layout

```text
<project-dir>/execution/
    journal/attempts/<attempt-id>/
        prepared.json                  # the assignment, the contract, and the frozen baseline
        start-permit.json              # written after the canonical RUNNING commit, before the host call
        launch.json                    # the host handle
        launch-failed.json             # the adapter reported that nothing started
        heartbeat/<seq>.json           # worker liveness, advisory only
        result.json                    # the writer's manifest, published last (§7.2)
        captures/worker/<check-id>-<seq>/
            capture.json · stdout.log · stderr.log
        captures/coordinator/<check-id>-<seq>/
            capture.json · stdout.log · stderr.log
        classification.json            # the coordinator's publication class (§7.3)
        hold/<cause>.json              # a recorded reason work must not proceed (§14)
        uncertainty/<cause>.json       # contact lost or a bound reached (§13.2)
        stop-evidence.json             # what established that execution stopped
        acceptance.json                # the receipt: what was executed and verified (§7.5)
        commit-observed.json           # the canonical revision observed after the accepting commit
        disposition.json               # the recorded resolution of every hold and uncertainty
        release.json                   # the registry grant was released; the attempt is resolved
    coordinator.lock/                  # ownership guard (§12.1)
    runtime.json                       # a cache of projections, never read for a decision (§5.3)
```

Inside the project directory, matching how every existing lock and file is scoped: it keeps a
project's execution record inside the directory that *is* its record, and keeps concurrent projects
from sharing a mutable file. `<attempt-id>` is opaque, unique, never reused, and constrained to
`[a-z0-9-]{8,64}` so it cannot escape this layout (§5.4) — a retried task has several attempt
directories, not one overwritten pair of timestamps.

**Every file above is written once, with the publish-if-absent primitive of §7.1, and never
rewritten.** There is no mutable record in the journal. `runtime.json` is the single exception and it
is a cache: §5.3 states what that means.

**One capture directory per check execution.** A task with two required checks, or one check that was
run twice, produces two capture directories, both preserved. `captures/worker/` belongs to the
assigned writer and is immutable once its `result.json` names it; `captures/coordinator/` belongs to
the coordinator and exists because a coordinator re-run happens *after* the worker's manifest is
sealed and therefore cannot be added to it (§11.4).

**Ownership within the store.** The coordinator owns every record above except `result.json`,
`heartbeat/` and `captures/worker/`, which belong to exactly one assigned writer — a worker, or the
capture helper acting for it (§11) — **for its own attempt**. That sentence scopes ownership inside
the execution store only: the same worker also writes the declared outputs its resource grant allows
(§10), which are not store records.

### 5.2 The journal, and the two predicates derived from it

The journal is the authority for three questions, each answered by a pure function over the *set of
record files present* in an attempt directory. No field is consulted to answer them, so a crash
between two writes can never leave two answers.

| Question | Answer |
|---|---|
| What label does this attempt display? | §6.1's ordered table |
| Does this attempt still hold registry claims? | **`prepared.json` present and `release.json` absent** |
| May the coordinator dispatch new work? | §7.3's `dispatch_blocked` predicate |

The second is the load-bearing one. **Claim-holding is independent of the label**, so an attempt that
displays `QUARANTINED`, `UNCERTAIN` or `STOPPED` still holds its reservations and still appears in
admission and takeover accounting. A label never removes an attempt from resource accounting; only
`release.json` does, and `release.json` is written only after the conditions of §13 are met.

Record kinds, their writers, and their multiplicity:

| Record | Writer | Multiplicity | Carries |
|---|---|---|---|
| `prepared` | coordinator | one | The contract (§6.5), the grant id, the mode, the deadline, the frozen baseline, and `supersedes` when it replaces an earlier attempt |
| `start-permit` | coordinator | one | The canonical revision and `ownership_generation` at which this attempt's `RUNNING` commit landed, and the host launch key if one exists |
| `launch` | coordinator | one | The host handle |
| `launch-failed` | coordinator | one | The adapter's definitive report that nothing started |
| `heartbeat/<seq>` | writer | many | Liveness only; **[UNENFORCED]** never progress (U6) |
| `result` | writer | one | The manifest (§7.2) |
| `captures/<owner>/<check-id>-<seq>/capture.json` | owner | many | One execution of one check (§11.3) |
| `classification` | coordinator | one | The publication class (§7.3) |
| `hold/<cause>` | coordinator | one per cause | Why work must not proceed (§14) |
| `uncertainty/<cause>` | coordinator | one per cause | Why the attempt's execution status is unknown |
| `stop-evidence` | coordinator | one | The evidence kind that established execution stopped (§13.2) |
| `acceptance` | coordinator | one | The receipt (§7.5) |
| `commit-observed` | coordinator | one | The revision observed after the accepting commit |
| `disposition` | coordinator | one | The recorded resolution: `retry`, `block`, `abandon`, or `accept_partial` |
| `release` | coordinator | one | That the registry grant was removed |

`hold` and `uncertainty` causes are a closed set, because the label function and the admission gate
switch on them:

```text
hold causes:         unreadable_record · invalid_identity · invalid_dependency ·
                     conflicting_publication · adequacy_failed · contract_invalidated ·
                     authorization_withdrawn · stop_requested · derivation_version_changed ·
                     superseded
uncertainty causes:  dispatch_interrupted · deadline · worker_gone
```

### 5.3 `runtime.json` is a cache

`runtime.json` holds `coordinator_run`, the projections a report wants (active attempt ids, held
claims, unresolved attempts, the computed `dispatch_blocked` value, host capacity, and the
case-sensitivity probe results of §8.2), and nothing else.

**It is never read to make a decision, and it is rebuilt from the journal and the registry on every
coordinator start.** A stale or missing `runtime.json` is therefore not a correctness problem and not
a recovery step: it is regenerated. This is the reason the dispatch pause of §14 is no longer a
durable flag whose absence could resume a stopped run — the pause is a predicate over records that
were written before the flag would have been.

`ownership_generation` is **not** in `runtime.json`. It is canonical (§5.5), because a fence that a
superseded coordinator can win a race against is not a fence (§12.2).

### 5.4 Canonical serialization, identifiers, and digests

Three records are content-addressed — the contract definition, `acceptance`, and each
`capture.json` — so their serialization is part of the protocol rather than an implementation detail.
One encoding applies to all of them:

- UTF-8, keys sorted lexicographically at every level, no insignificant whitespace, `ensure_ascii`
  false, and `\n` for the trailing byte if any.
- **Numbers are integers or strings only.** No floating-point value appears in a content-addressed
  record: durations are integer milliseconds, sizes are integer bytes, and anything else that would be
  fractional is a decimal string. This removes shortest-round-trip, negative-zero and non-finite
  questions instead of specifying answers to them.
- Absent optional values are **omitted**, never `null`, so "absent" and "null" cannot hash alike.
  A field whose table entry says "or null" is present-and-null and means something specific.
- Duplicate object keys are a parse error, not a last-wins merge.
- A digest is lowercase hex `sha256` over those bytes, written as `"sha256:<64 hex>"`.

Identifier syntax, so that no id can escape the layout of §5.1:

| Id | Syntax |
|---|---|
| `attempt-id`, `grant-id`, `capture-id` | `[a-z0-9][a-z0-9-]{7,63}` |
| `check-id` | `[a-z0-9][a-z0-9_-]{0,31}`, unique within a contract |
| `receipt-id` | `sha256:<64 hex>`, the digest of the `acceptance` record with `receipt` omitted |
| `sequence` | A decimal integer, zero-padded to 4 digits, unique within its check and owner |

Every id is validated on read as well as on write. A record whose id does not match its own directory
name is unreadable (§5.5), not merely wrong.

### 5.5 Schema versions, the canonical gate, and migration

**Store records.** Every record's `schema` is `<name>/<major>.<minor>`, and the rules are fail-closed:

- A reader accepts a record whose `major` it knows, and within that major ignores fields it does not
  recognise, so a `minor` bump is additive and backward-compatible.
- A reader that meets an **unknown `major`**, JSON that does not parse, a digest that does not match,
  or an id that does not match its location treats the record as **unreadable, not absent.** It writes
  `hold/unreadable_record` for that attempt and reports. Treating an unreadable record as a missing one
  is how a second worker gets launched for work that already ran.
- `major` increases only when a field's meaning changes or a required field is added.
- Conditional requiredness is part of each schema and is stated in the tables of §6 and §7 rather than
  left to prose: a field marked "required when *X*" is rejected when present without *X* and rejected
  when absent with it.

**Canonical state.** A project that has ever enabled this protocol is not safe to operate with an
installed host that predates it: such a host reads `project.json` happily, ignores `execution/`
entirely, and can commit over active attempts. So enabling the protocol raises the project's
`schema_version` from 3 to **4**, and an older installation then refuses the project outright —
`detect_schema_version` reads the integer and validation returns `unsupported schema_version` for
anything it does not implement [C12]. That refusal is the fail-closed marker; a sidecar file in
`execution/` would not be one, because the old reader never looks there.

**Schema version 4 is version 3 plus one project-level block:**

```json
"execution": {
  "protocol": "parallel-execution/1",
  "coordinator_run": "<opaque string, or null>",
  "ownership_generation": 0,
  "attempts": { "<task-id>": "<attempt-id>" }
}
```

`attempts` maps each currently-`RUNNING` task to the exact attempt that was permitted to execute it,
which is what lets recovery bind a canonical status to an attempt instead of inferring one (§6.3), and
what makes §12.2's fence a revision-checked commit rather than a check.

**Migration is an explicit operation, not a field write.** `schema_version` is immutable to the
ordinary commit path and transactional commits currently require exactly 3 [C21], so the version bump
cannot be smuggled into a candidate. The operation is:

```sh
research-project enable-execution <project-dir> --expected-revision <revision>
```

- It takes the project lock, requires **no `RUNNING` tasks**, raises `schema_version` to 4, inserts the
  `execution` block with `ownership_generation: 0` and empty `attempts`, and increments the revision —
  in **one** revision-checked atomic replacement, so an interruption either did nothing or did all of
  it. There is no partially migrated state to recover.
- It preserves every other field verbatim, including terminal task history and authorization records.
- It is refused on a project that is not `PLANNING` or `EXECUTING`, and refused twice: a project
  already at 4 reports that and exits zero.
- Reopening a v4 project leaves it at 4. Closure does not downgrade it, because a downgrade would
  reintroduce exactly the unsafe-old-installation case the bump exists to prevent.

**Rollout order is a correctness requirement, not a preference.** Readers understand version 4 in
Stage 4, the migration operation ships in Stage 6, and dispatch is enabled in Stage 7 (§19). Adding the
gate in the activation stage would mean the first project to migrate is also the first project whose
graph loader, allocator, validator and report have never seen version 4.

### 5.6 Durability classes

| Content | Class | Why |
|---|---|---|
| `runtime.json` | **Disposable** | Rebuilt from the journal and the registry on every start (§5.3) |
| A mirror of task ids, levels, statuses | **Disposable** | Reconstructible from `project.json` at any revision |
| `prepared` for an attempt with no `start-permit` | **Disposable** | No host call can have happened, because the permit strictly precedes it (§6.3). Re-preparable |
| `prepared` for an attempt with a `start-permit` | **Durable** | It is the only record of what a possibly-running worker was told to do |
| `start-permit` | **Durable** | Its presence is the only thing that distinguishes "may have launched" from "cannot have launched" |
| `launch` | **Durable** | It carries the handle; losing it loses the ability to observe or interrupt |
| A registry grant | **Durable** | It is the only evidence that a resource is claimed by an attempt that may still be writing |
| **A published `result` not yet classified** | **Durable** | Canonical state cannot reconstruct it. Losing it discards work that was actually performed |
| **A capture not yet imported into evidence** | **Durable** | It attests an execution that happened once |
| `hold` and `uncertainty` | **Durable** | They are the dispatch gate (§7.3). Losing one resumes a run that a failure stopped |
| `acceptance` | **Durable, immutable** | It is the identity a restart uses to recognise already-accepted work, and reusing it verbatim is what makes a retry idempotent (§7.5) |
| `commit-observed` | **Durable, immutable** | It carries the revision at which the receipt was observed accepted |
| `disposition`, `stop-evidence`, `release` | **Durable, immutable** | They are the record that reconciliation actually happened before claims were dropped |

**A store need not be authoritative for task status to hold irreplaceable evidence.** Any later
substrate inherits that obligation: retention and recovery rules, not just a schema.

### 5.7 Retention and collection

Durability says what a crash must not lose. Retention says what a *later* operation may remove, and
they are different questions.

- **Nothing under `execution/journal/` is removed while the project is not `DONE` or `CANCELLED`.**
  Collection is a closure activity, never a scheduling one. Removing the empty `coordinator.lock/`
  directory during normal release is not collection; it is outside `journal/` for exactly that reason.
- **Collection is tombstoned before it deletes.** The coordinator appends one `collection` record to
  the project's evidence naming every path it is about to remove and the reason, commits it, and only
  then removes them. A missing file named by a tombstone is *collected*; a missing file with no
  tombstone is a `WorkspaceError` and the project does not resume dispatch until an operator
  reconciles it. A crash between the tombstone and the deletion leaves a tombstone naming files that
  still exist, which the next closure re-runs idempotently.
- **An `acceptance` record referenced by canonical evidence is never collected**, and neither is any
  capture or log it names, transitively.
- **[UNENFORCED]** `_validate_evidence_reference` checks that a *directly referenced* evidence file
  exists [C13]. It does not follow a receipt's references to its captures and logs, so the transitive
  rule above is not enforced by current validation and is an explicit Stage 4 task (§19). Revision 3
  claimed this rule was mechanically observable today; it is not.
- **Reopening a closed project may find collected records.** A reopened project's earlier attempts are
  historical: their tombstones stay, and no new attempt reuses an attempt id, so a collected record is
  never a hole in live state.
- **Logs are bounded at capture time, not at collection time.** A capture records at most
  `max_log_bytes` per stream (default 262144), sets `truncated`, and hashes **the bytes it retained** —
  the digest attests the stored log, not the log that was produced, and the record says so. Captures
  are subject to the same redaction rule as every other workspace record: secrets, credentials and
  tokens are redacted before the bytes are written, so the digest covers the redacted content and
  there is no second, unredacted artifact to protect.

## 6. Attempts

### 6.1 Labels derived from the journal

An attempt has no stored state field. Its **label** is a pure function of which record files exist,
evaluated top to bottom, first match wins. `prepared.json` is a **precondition of the whole table**: a
directory without it is not an attempt, and it is reported as an unreadable journal rather than given a
label.

| # | Label | Condition (records present / absent) |
|---|---|---|
| 1 | `UNCERTAIN` | any `uncertainty/*`, and no `stop-evidence` |
| 2 | `ABANDONED` | `disposition` with resolution `abandon` or `retry` |
| 3 | `RECONCILED` | `disposition` with resolution `accept_partial` or `block` |
| 4 | `INTEGRATED` | `acceptance` and `commit-observed` |
| 5 | `QUARANTINED` | any `hold/*`, and no `disposition` |
| 6 | `STOPPED` | `stop-evidence`, and no `disposition` |
| 7 | `LAUNCH_FAILED` | `launch-failed` |
| 8 | `VERIFIED` | `acceptance`, and no `commit-observed` |
| 9 | `RESULT_READY` | `result` and `classification`, and no `acceptance` |
| 10 | `PUBLISHED` | `result`, and no `classification` |
| 11 | `RUNNING` | `launch` and at least one `heartbeat`, and no `result` |
| 12 | `DISPATCHED` | `launch`, and no `heartbeat` and no `result` |
| 13 | `DISPATCHING` | `start-permit`, and no `launch` |
| 14 | `PREPARED` | `prepared` only |

**Fourteen rows, fourteen labels, one row each.** `PREPARED`, `DISPATCHING`, `DISPATCHED`, `RUNNING`,
`PUBLISHED`, `RESULT_READY`, `VERIFIED`, `INTEGRATED`, `LAUNCH_FAILED`, `UNCERTAIN`, `QUARANTINED`,
`STOPPED`, `RECONCILED`, `ABANDONED`. The bijection is deliberate: a label that needed two rows would
mean the table was carrying a second variable, and the second variable is the one below.

**Finalization is a separate predicate, not a label:**

```text
finalized     ⟺  release present
holds_claims  ⟺  prepared present  ∧  ¬finalized
```

Rows 2, 3, 4 and 7 are the four **finalizable** conditions. An attempt in one of them with `release`
still absent is *pending finalization*: it displays the same label and it still holds its claims, which
is what the `*` suffix means in §6.2. §6.3 makes completing it the next coordinator's first obligation.

Revision 3 folded `release` into the label conditions themselves, and the consequence is worth
recording because the model test is what found it: three of the four finalization-pending record sets —
a disposition written but not yet released, and an accepted attempt whose commit was observed but not
yet released — matched **no row at all**, so precisely the crash window between the disposition and the
release had no label. Separating the two variables removes the window rather than adding rows for it.

Row 14's `only` is load-bearing and it is not an oversight that it excludes `release`. `release` is
written by T16 and T24 alone, and neither has `PREPARED` as a source: a grant taken by T1 and never
dispatched is released by quarantining the attempt (T25–T27) and disposing of it (T20–T23), not by
dropping the grant where nothing records why. So `{prepared, release}` is unreachable, and the model
test asserts that the table reports it as unreadable rather than labelling it.

**A record set that matches no row is still not given a label.** It is reported as an unreadable journal
and the attempt is treated as claim-holding (because `prepared` exists) and unresolved. Inventing a
label for an unexpected combination is how a half-written transition becomes a dispatch decision.

Four consequences worth stating explicitly, because they are what the ordering buys:

- **`uncertainty` outranks everything.** Row 1 fires while execution status is unknown, because
  "a writer may still be running" is the fact that decides whether anything else may touch those paths.
  Once `stop-evidence` exists the attempt falls through — to rows 2–4 if it was disposed, to row 5 if a
  hold is outstanding, to row 6 if not. So the unknown is always visible as its own label rather than
  hidden behind a hold.
- **`hold` outranks progress.** An attempt whose result was published and then found to have an
  invalid dependency is `QUARANTINED` (row 5) rather than `PUBLISHED` (row 10), and it stays there
  until a `disposition` is written. "Progress" means rows 8–13 — the states whose canonical status is
  still `RUNNING`. It does *not* mean rows 2–4 and 7: those are above row 5, and §6.2 bounds every
  hold row's source so that a hold is never written into one, because there it would be shadowed.
- **`QUARANTINED` and `STOPPED` are not finalizable.** Neither can carry `release` — a disposition is
  required first — so both still hold claims (§5.2), which is exactly what revision 3 got wrong by
  making quarantine terminal for scheduling while a live writer might remain.
- **A `disposition` is written only from `QUARANTINED` or `STOPPED`** (§6.2, T20–T23), never from
  `UNCERTAIN`. Row 1 outranking rows 2–4 is what makes that a table property rather than a convention:
  a disposition written while an uncertainty is outstanding does not produce a resolved label.

### 6.2 Transitions

Each row is one *record write*, since a record write is the only thing that changes a label. "Canonical"
names the `project.json` change that accompanies it, and every canonical value in this table is drawn
from `TASK_STATUSES` and permitted by `TASK_TRANSITIONS` [C20] — the doc test re-reads both from
`workspace_lib.py` and rejects any cell that is not.

| # | From | Record written | To | Canonical | Trigger |
|---|---|---|---|---|---|
| T1 | — | `prepared` | `PREPARED` | none | Admission passed (§8.1), grant inserted (§8.5) |
| T2 | `PREPARED` | `start-permit` | `DISPATCHING` | `TODO → RUNNING` | Canonical commit landed; the permit records its revision and generation |
| T3 | `DISPATCHING` | `launch` | `DISPATCHED` | none | Adapter returned `started(handle)` |
| T4 | `DISPATCHING` | `launch-failed` | `LAUNCH_FAILED`\* | `RUNNING → TODO` | Adapter returned `definitively_not_started` |
| T5 | `DISPATCHING` | `uncertainty/dispatch_interrupted` | `UNCERTAIN` | none | Adapter returned `ambiguous`, or recovery found a permit with no launch record |
| T6 | `DISPATCHED` | `heartbeat/0001` | `RUNNING` | none | First worker heartbeat |
| T7 | `DISPATCHED`, `RUNNING` | `result` | `PUBLISHED` | none | Worker published its manifest (§7.2) |
| T8 | `PUBLISHED` | `classification` (`valid_success` or `valid_failed`) | `RESULT_READY` | none | Classifier accepted the publication (§7.3) |
| T9 | `PUBLISHED` | `hold/unreadable_record` | `QUARANTINED` | none | Manifest unparseable, mis-digested, or mis-located |
| T10 | `PUBLISHED` | `hold/invalid_identity` | `QUARANTINED` | none | Contract hash, attempt id or grant id does not match |
| T11 | `PUBLISHED` | `hold/invalid_dependency` | `QUARANTINED` | none | The manifest names a task, output or check the contract did not |
| T12 | `PUBLISHED` | `hold/conflicting_publication` | `QUARANTINED` | none | A record already exists with differing bytes (§7.1) |
| T13 | `RESULT_READY` | `acceptance` | `VERIFIED` | none | Per-check adequacy satisfied (§11.5) |
| T14 | `RESULT_READY` | `hold/adequacy_failed` | `QUARANTINED` | none | A required check has no qualifying capture |
| T15 | `VERIFIED` | `commit-observed` | `INTEGRATED`\* | `RUNNING → DONE`, or `RUNNING → BLOCKED` for `valid_failed` | The accepting canonical commit landed and its revision was observed |
| T16 | `INTEGRATED`\*, `LAUNCH_FAILED`\* | `release` | `INTEGRATED`, `LAUNCH_FAILED` | none | Grant removed; finalization complete |
| T17 | `DISPATCHED`, `RUNNING` | `uncertainty/worker_gone` | `UNCERTAIN` | none | Heartbeat bound exceeded (§13.2) |
| T18 | `DISPATCHED`, `RUNNING` | `uncertainty/deadline` | `UNCERTAIN` | none | Contract deadline passed with no result |
| T19 | `UNCERTAIN` | `stop-evidence` | `STOPPED` | none | Execution demonstrably stopped (§13.2), or `discover` reported `never_started` |
| T20 | `STOPPED`, `QUARANTINED` | `disposition` (`block`) | `RECONCILED`\* | `RUNNING → BLOCKED` | Operator or rule chose to block the task |
| T21 | `STOPPED`, `QUARANTINED` | `disposition` (`accept_partial`) | `RECONCILED`\* | `RUNNING → BLOCKED` | Partial outputs reconciled and recorded as evidence (§13.3) |
| T22 | `STOPPED`, `QUARANTINED` | `disposition` (`retry`) | `ABANDONED`\* | `RUNNING → TODO` | Work will be re-attempted under a new attempt id |
| T23 | `STOPPED`, `QUARANTINED` | `disposition` (`abandon`) | `ABANDONED`\* | `RUNNING → SKIPPED` with a `skip_reason` | The task will not be attempted again |
| T24 | `RECONCILED`\*, `ABANDONED`\* | `release` | `RECONCILED`, `ABANDONED` | none | Grant removed; finalization complete |
| T25 | `PREPARED` | `hold/contract_invalidated` | `QUARANTINED` | none | A `definition` input changed before the permit was written (§6.6) |
| T26 | `PREPARED`…`RUNNING` | `hold/authorization_withdrawn` | `QUARANTINED` | none | Authorization ceased to be in force (§14.1) |
| T27 | `PREPARED`…`RUNNING` | `hold/stop_requested` | `QUARANTINED` | none | An operator asked for a stop; not a failure (§14.1) |
| T28 | `PREPARED`…`VERIFIED`, `UNCERTAIN`, `STOPPED` | `hold/superseded` | `QUARANTINED` | none | A replacement attempt names this one in `prepared.supersedes` |
| T29 | `PREPARED`…`VERIFIED`, `UNCERTAIN`, `STOPPED` | `hold/derivation_version_changed` | `QUARANTINED` | none | Takeover found a `derivation_version` it does not implement (§6.6) |

\* A row whose "To" ends in `*` is a finalizable condition of §6.1 (rows 2, 3, 4 and 7) reached with
`release` still absent — that is, `¬finalized`. The label is the same name either way, and the attempt
still holds its claims until `release` is written; §6.3 explains why that is not an ambiguity.

**No hold may be written into a finalizable condition.** T28 and T29 said "any unresolved" in
revision 3, and `INTEGRATED`\* and `LAUNCH_FAILED`\* are unresolved in the `¬finalized` sense, so the
rule as written permitted a hold there. Rows 4 and 7 of §6.1 precede row 5, so such a hold would be
*shadowed*: the label would stay `INTEGRATED`, §6.3 would still call for `release`, and the hold would
have no effect on any decision. It cannot be fixed by reordering §6.1 either, because T20–T23 all move
canonical status from `RUNNING`, and an `INTEGRATED` attempt's canonical status is already `DONE` or
`BLOCKED` — a disposition written there would be rejected by `TASK_TRANSITIONS` [C20]. So the source
lists are bounded instead: every hold row's source is a state whose canonical status is still `RUNNING`
(or, for T25, still `TODO`), and the only record a finalizable attempt can still receive is `release`.
The model test asserts this as a property — that writing a hold from any permitted source changes the
label to `QUARANTINED`, or leaves it `UNCERTAIN` — rather than as a convention.

**T22 and T28 are the supersession pair, and neither depends on observing a successor's outcome.**
A retry writes `disposition(retry)` and `release` on the old attempt *before* preparing the new one,
and the new attempt's `prepared.supersedes` names the old one, so recovery can tell "superseded" from
"still live" by reading records rather than by inferring intent from another attempt's progress.

### 6.3 Crash prefixes and finalization

The protocol writes several files per transition, so a crash can land between any two of them. The
design rule is: **for every prefix of every multi-file transition, the record set present determines
one outcome, and that outcome is reached by completing the transition rather than by guessing.**

The load-bearing case is dispatch, and it is made deterministic by *ordering the permit against the
host call*:

```text
canonical RUNNING commit  →  write start-permit  →  re-read generation  →  host call  →  write launch
```

- Crash **before** the canonical commit: no `start-permit`. No host call can have happened, so
  `prepared` is disposable (§5.6); the coordinator re-prepares or releases.
- Crash **after** the canonical commit, before the permit: the task is canonically `RUNNING` with an
  attempt in `execution.attempts` but no permit. Still no host call. Recovery writes the permit and
  proceeds, or commits `RUNNING → TODO` and releases.
- Crash **after** the permit, before `launch`/`launch-failed`: this is the one genuinely ambiguous
  window, and it is resolved with evidence rather than assumption — `discover(launch_key)` (§17), and
  failing that `uncertainty/dispatch_interrupted` (T5) and no new dispatch for that task until
  `stop-evidence` exists.

Every other multi-file transition is made deterministic the same way, by writing the record that
*permits* the next irreversible act before performing it:

| Transition | Prefix seen | Deterministic outcome |
|---|---|---|
| Accept and commit (T15) | `acceptance`, no `commit-observed` | Re-read canonical state; the receipt is already in evidence (commit landed) → write `commit-observed`; not present → retry the same commit with the same `acceptance` bytes (§7.5) |
| Finalize (T16, T24) | Finalizable condition, no `release` | Re-run the required canonical commit idempotently, then write `release`. Both steps tolerate having already happened |
| Launch failure (T4) | `launch-failed`, canonical still `RUNNING` | Commit `RUNNING → TODO`, then `release`. The commit is idempotent because the target status is checked first |
| Reconcile (T20–T23) | `disposition`, no `release` | Apply the disposition's canonical status if not already applied, then `release` |
| Collection (§5.7) | tombstone, files still present | Re-remove the named paths; a missing named path is collected, not lost |

**Finalization is a start-up obligation, not a background job.** A coordinator's first action after
taking ownership (§12.1) is to scan the journal, complete every pending finalization, and only then
consider admission. That is why `release` is a separate predicate from the label (§6.1): an attempt
still holding a grant is still in resource accounting whatever it displays, so completing finalization
is what frees capacity, and doing it before admission means a restart cannot dispatch into resources a
dead run never released.

### 6.4 Attempt identity

`<attempt-id>` is opaque and never reused. It is *not* derived from the task id, so a task retried
after a failure has two attempt directories and two independent records — necessary because the first
attempt's worker may still be alive, and its records must not be overwritten by the second's.

An attempt names its task; a task does not name its attempt except through canonical
`execution.attempts`, which holds only the currently permitted one. Historical attempts are found by
scanning the journal, which is exactly what recovery does.

### 6.5 The contract object

`prepared.json` embeds the **contract** — everything the worker is told and everything the coordinator
later checks a result against.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema` | string | yes | `contract/1.0` |
| `attempt_id` | string | yes | §5.4 syntax |
| `task_id` | string | yes | The canonical task |
| `project_revision` | integer | yes | The revision the contract was derived from |
| `derivation_version` | integer | yes | The derivation rule set (§6.6) |
| `grant_id` | string | yes | The registry grant backing this attempt (§8.5) |
| `mode` | string | yes | `shared` or `copy` (§9) |
| `working_directory` | string | yes | Absolute, resolved, existing |
| `outputs` | array | yes | Declared output objects, each `{path, root}` with `root ∈ {target, workspace, workspace_root, external}` |
| `claims` | array | yes | The claim set of the grant, restated so a worker can self-check |
| `checks` | array | yes | Resolved checks (§11.2). May be empty only when the task's `verification` is empty |
| `checks_interpretation` | object | yes | `{rule_version, source_text, resolution}` — how `verification` became `checks` |
| `effect` | object | yes | The task's `effect`, copied verbatim |
| `authorization` | object | yes | The task's `authorization`, copied verbatim, plus `frozen_scope` |
| `deadline_ms` | integer | yes | Wall-clock budget; integer milliseconds (§5.4) |
| `heartbeat_interval_ms` | integer | yes | How often the writer is asked to beat; the lost-contact bound is derived from it (§13.2) |
| `max_log_bytes` | integer | yes | Per-stream capture cap (§5.7) |
| `definition_hash` | string | yes | Digest over the `definition` identity set (§6.6) |
| `baseline` | object | yes | The frozen input baseline (§6.6) |
| `supersedes` | string | no | An attempt id this one replaces (§6.2) |
| `host` | object | yes | `{adapter, adapter_version, capabilities}` from §17 |

Nested requiredness, so validation is mechanical rather than interpretive: `outputs[].path` is
relative to its root and may not contain `..` or a symlink component; `claims[]` matches §8.2's
shape; `checks[]` matches §11.2's; `authorization.frozen_scope` is required when
`authorization.status` is `explicit` and forbidden otherwise; `host.capabilities` is a closed set of
booleans.

`definition_hash` covers the **definition** identity set only. It does not cover `baseline`, and that
is the whole point of §6.6.

### 6.6 Derivation, and the three identity sets

A contract is derived from canonical state plus the filesystem. Three sets are kept apart because
they answer three different questions, and revision 3's single `input_digests` conflated two of them.

**1. `definition` — what the task *is*.** Task id, name, `depends_on`, `outputs`, `success_criteria`,
`verification`, `effect`, `authorization` (minus timestamps), the resolved `checks`, the claim set,
the mode, and `derivation_version`. `definition_hash` is the digest of this set, and it is **the only
thing recomputed to test invalidation**: if a re-derivation at dispatch time produces a different
`definition_hash`, the plan changed under the attempt and T25 fires.

**2. `baseline` — what the inputs looked like when the attempt started.** Captured **after** the
registry grant is inserted (so no other claim-holder can be mid-write) and **never recomputed**:

```json
"baseline": {
  "captured_at": "<RFC 3339 UTC>",
  "inputs_enumerated": true,
  "read_only_inputs": [ {"path": "...", "root": "target", "digest": "sha256:...", "size": 1234} ],
  "writable_subjects": [ {"path": "...", "root": "target", "digest": "sha256:...", "state": "present"} ]
}
```

- **A path the task will write is a `writable_subject`, never a `read_only_input`.** This is the rule
  that fixes revision 3's defect where editing an existing file invalidated the attempt's own
  contract: writable subjects have their pre-state recorded for evidence and reconciliation, and
  their change is *expected*, so it invalidates nothing.
- A subject that does not exist yet is recorded with `state: "absent"` and no digest, so "the file
  appeared" and "the file changed" are distinguishable.
- `read_only_inputs` are the declared outputs of the tasks in `depends_on`, resolved to paths. A
  digest mismatch on one of those at dispatch time is a **prerequisite changed** condition, reported
  as `hold/invalid_dependency`, not as contract invalidation — different cause, different disposition.
- **`inputs_enumerated: false` is the honest fallback.** When a dependency's outputs cannot be
  enumerated (a directory tree, an external system, an artifact this protocol will not hash — §5.7),
  the baseline says so and no input-identity guarantee is claimed for that dependency. Silently
  hashing whatever happened to be listed would be a guarantee the protocol cannot keep.
- Hashing is restricted by construction: regular files only, resolved with symlinks refused (a
  symlink component in a declared path is a derivation error), no directory hashing, no external
  artifact hashing. Deferred, not promised — §21 lists what would be needed.

**3. `produced` — what the writer actually wrote.** Digests in `result.json` (§7.2), compared against
`baseline.writable_subjects` at classification. A subject whose digest is unchanged and whose state
was `present` is reported as untouched; an undeclared path with a changed digest is
`hold/invalid_dependency`.

**Case sensitivity is probed, not assumed.** Two declared paths that differ only in case are a
derivation error on a case-insensitive volume and legal on a case-sensitive one, so the coordinator
probes each distinct volume once per run (create `a`, stat `A`), caches the result in `runtime.json`
(§5.3), and uses the probe for that volume. Hardlink aliasing is **not** detected: two claims may
name the same inode through different paths and the protocol will not notice. That is stated as a
limitation in §21 rather than left implied.

## 7. Publication and acceptance

### 7.1 The publish-if-absent primitive

`atomic_write_text` is correct for replacing a file [C9] and wrong for publishing a record, because
`os.replace` overwrites unconditionally. Every journal record is published with a primitive that
**refuses to overwrite**:

```text
fd = os.open(tmp, O_CREAT | O_EXCL | O_WRONLY, 0o600)   # tmp is unique to this writer
write(fd, bytes); os.fsync(fd); os.close(fd)
os.link(tmp, final)          # raises FileExistsError; never replaces
os.unlink(tmp)
fsync(dirfd(final))          # the link is durable before the caller proceeds
```

`os.link` is the load-bearing call: it fails rather than clobbering. On `FileExistsError` the writer
**reads the existing record and compares bytes**:

- identical → the previous attempt at this write succeeded; treat as success and continue. This is
  what makes every publication idempotent under retry.
- different → `hold/conflicting_publication` (T12). Two writers disagree about what happened, and
  nothing but an operator should decide which is right.

**Host prerequisite.** Atomic, `EEXIST`-returning `link(2)` on the volume holding the project
directory is a §17 requirement. It holds on APFS, HFS+, ext4, XFS and btrfs. It is **not** reliable on
some NFS configurations, where a lost reply can leave a successful link reported as `EEXIST`; the
byte-comparison above makes that case *correct* (identical bytes → success) rather than merely
survivable, but a host that cannot promise atomic `link` must declare `atomic_link: false` and is
refused for parallel execution.

Multi-file records (a capture directory, `prepared` plus its logs) publish their contents first and
their **manifest last**, so the manifest's presence is what makes the record exist. A crash mid-write
leaves an unnamed partial directory, which collection removes and which no reader ever consults.

### 7.2 The protocol

```text
worker:      writes declared outputs           (only inside its claims)
worker:      writes captures/worker/<check>-<seq>/ contents, then capture.json
worker:      publishes result.json             ← the manifest, last, publish-if-absent
coordinator: classifies                        (§7.3, writes classification or a hold)
coordinator: runs its own checks                (every check with executed_by: coordinator, §11.6)
coordinator: selects qualifying captures        (§11.5)
coordinator: publishes acceptance.json          (§7.5, publish-if-absent, reused verbatim on retry)
coordinator: commits canonical state            (revision-checked; imports evidence)
coordinator: publishes commit-observed.json     (the revision it read back)
coordinator: releases the grant                 (§8.5, then release.json)
```

`result.json` last is the ordering that makes a crashed worker distinguishable from a finished one:
the manifest exists only if everything it names was written first.

### 7.3 The publication classifier, and the dispatch gate

Classification is a **total function** over a publication, evaluated in this order, with exactly one
transition per class:

| Order | Class | Condition | Transition |
|---|---|---|---|
| 1 | `absent` | No `result.json` | none (the attempt is still pre-publication) |
| 2 | `unreadable` | Unparseable, digest mismatch, unknown `major`, or id/location mismatch | T9 |
| 3 | `invalid_identity` | `attempt_id`, `grant_id` or `definition_hash` does not match `prepared` | T10 |
| 4 | `invalid_dependency` | Names a task, output, check or path the contract did not; or a `read_only_inputs` digest changed | T11 |
| 5 | `conflicting` | A differing record already exists at a record path (§7.1) | T12 |
| 6 | `valid_failed` | Readable, identity and references correct, and the manifest reports failure | T8 |
| 7 | `valid_success` | Readable, identity and references correct, and the manifest reports success | T8 |

`invalid_dependency` **quarantines**; it is never "re-read later". A publication that names something
outside its contract is a protocol violation, not a timing artifact, and re-reading it in a loop is
how a broken worker becomes a hot loop.

**The dispatch gate is derived, not flagged.** New dispatch is blocked while any of these holds, and
each is a journal query, not a stored boolean:

```text
dispatch_blocked  ⟺  ∃ attempt with a result, no classification and no hold   (unclassified failure)
                  ∨  ∃ attempt with a hold and no disposition                  (unresolved hold)
                  ∨  ∃ attempt with an uncertainty and no stop-evidence        (possible live writer)
                  ∨  the canonical schema_version ≠ 4                          (§5.5)
                  ∨  ownership is not held at the current generation            (§12)
```

The `no hold` conjunct in the first clause is load-bearing, and it is there because the model test
deadlocked without it. Rows 2–5 of the classification table quarantine a publication **instead of**
classifying it, so `result ∧ ¬classification` stays true for a quarantined publication forever. Without
the conjunct, disposing of the hold cleared the second clause and the first one kept dispatch blocked
for the life of the project. With it, each of the three journal clauses is cleared by the write that
resolves the condition it names, and by nothing else.

That definition removes revision 3's ordering hazard, where a `valid_failed` publication set a pause
flag *after* the pause was consulted, and where a crash between the two lost the pause entirely. Here
the blocking condition is created by the *same write* that creates the failure — the `result` — so
there is no window and nothing to lose. `runtime.json` caches the computed value for reporting only
(§5.3).

`dispatch_blocked` stops **new** dispatch. It does not interrupt running attempts, does not stop
finalization (§6.3), and does not stop classification or reconciliation — the work that clears it must
be able to run while it is set.

### 7.4 Three records, three purposes

| Record | Written by | Answers |
|---|---|---|
| `result.json` | the writer | What did the worker claim it did? |
| `captures/*/capture.json` | the check's owner | What did one execution of one check actually produce? |
| `acceptance.json` | the coordinator | What is being integrated, and on what basis? |

`result.json` fields: `schema`, `attempt_id`, `task_id`, `grant_id`, `definition_hash`, `outcome`
(`success` or `failed`), `failure` (required when `outcome` is `failed`: `{kind, detail}` with
`kind ∈ {check_failed, command_error, precondition, refused, internal}`), `produced` (digests per
§6.6), `captures` (relative paths to the capture directories the worker wrote), `notes` (bounded free
text), `started_at`, `finished_at`.

A worker's *self-assessment* of success is advisory. `outcome: success` with a capture showing a
failing check classifies as `valid_success` and then fails adequacy (T14), because the coordinator
reads the captures rather than the claim.

### 7.5 Acceptance and idempotence

`acceptance.json` is at **one deterministic path per attempt**, published if-absent (§7.1), and
carries:

`schema`, `attempt_id`, `task_id`, `receipt_id`, `accepted_at`, `definition_hash`, `classification`,
`qualifying_captures` (one capture id per required `check_id`), `superseded_captures` (every other
capture considered, retained so a failed-then-passing history is not erased), `produced`,
`evidence_refs` (the paths imported into `evidence.md`), and `canonical_intent` (the task status the
accepting commit will set).

Idempotence follows from three rules:

1. **`accepted_at` is written once and reused verbatim.** A retry re-reads the existing record rather
   than re-deriving a timestamp, so the receipt's identity does not change between attempts to commit
   it. This is the concrete fix for revision 3, where a receipt whose id incorporated the current
   time produced a *different* receipt on every retry and therefore duplicate evidence.
2. **`receipt_id` is the digest of the record with `receipt_id` omitted** (§5.4). It is a function of
   content, not of when it was computed, so the same acceptance always yields the same id.
3. **The canonical evidence reference is read before any commit retry.** A receipt already present in
   canonical evidence means the commit landed; the coordinator writes `commit-observed` and stops. A
   receipt absent means the commit did not land and is retried with byte-identical content.

`commit-observed.json` carries `observed_revision` — the revision the coordinator read *after* its
commit was accepted. It is deliberately not called "the accepting revision": a concurrent commit can
land between the accepting commit and the read-back, so `observed_revision` is an upper bound on where
the receipt first appeared, and any report that presents it as exact is wrong. What it is good for is
recovery: a receipt present at or before `observed_revision` proves the accepting commit happened.

## 8. Scheduling

### 8.1 Admissibility

A task is admissible when **all** of the following hold. The list is the whole gate; there is no
implicit condition.

1. Status is `TODO`, and every id in `depends_on` is `DONE`.
2. `dispatch_blocked` is false (§7.3).
3. **The work is delegable**, decided by one predicate over canonical fields:
   ```text
   delegable  ⟺  effect.kind ∈ {none, local_write}  ∧  ¬authorization.required
                 ∧  authorization.status == "not_required"
   ```
   `effect.kind` is drawn from `EFFECT_KINDS` [C10] and the statuses from
   `AUTHORIZATION_STATUSES` [C17]; `required: false` with any status other than `not_required` is
   already rejected by validation [C18], so the third clause is a restatement rather than a new rule.
   A task that is not delegable runs **inline on the coordinator** (§8.5 still applies), because
   destructive and external effects need the human in the loop that only the coordinator has.
4. Authorization is **in force**, by the companion predicate:
   ```text
   in_force  ⟺  (¬required ∧ status == "not_required")
              ∨  (required ∧ status == "explicit" ∧ scope == the scope frozen in the contract)
   ```
   `pending`, `denied` and `deferred` are all **not** in force: `pending` means nobody has authorized
   it, `denied` means someone refused, `deferred` means the decision was postponed. None of the three
   may execute, and the difference between them is reported rather than collapsed. A `required` task
   is committed `RUNNING` only when `explicit` is in force, matching validation, which rejects a
   `RUNNING` or `DONE` required task whose status is not `explicit` [C19].
5. Every claim in the computed set can be granted (§8.5) without conflict.
6. Host capacity permits another worker (§8.4).
7. Every declared output path is writable, contains no `..` and no symlink component, and lies under
   its declared root — checked against `REFERENCE_ROOTS` semantics [C11].

**A `BLOCKED` task is not admissible, and retrying one is a separate canonical step.** Condition 1 is
`TODO` and nothing normalizes a status on the way into admission, because a candidate that quietly
rewrote `BLOCKED` to `TODO` would erase the reason the task was blocked. Retry is an explicit
canonical commit `BLOCKED → TODO` — legal under `TASK_TRANSITIONS` [C20] — that carries forward the
previous block reason in the task's notes; only then does the task reach condition 1. `T22`'s
`RUNNING → TODO` is the same rule from the other side: a retried attempt hands the task back in
`TODO`, so the next attempt passes the same gate as the first rather than a weaker one.

### 8.2 Resources

Every claim is `{namespace, key, access}` with `access ∈ {read, write}` and **two namespaces only**:

| Namespace | Key | Meaning |
|---|---|---|
| `path` | An absolute, resolved filesystem path | A file or a subtree |
| `external` | A stable opaque name declared by the plan | An outside system: an API, a remote branch, a deployment target |

Revision 3's third namespace (`exclusive`) is **removed**, because it created the asymmetry the review
identified: a repository-wide `exclusive` claim did not conflict with an ordinary `path` write to a
file inside that repository, so two writers could hold "the whole repository" and "one file in it"
simultaneously. A repository-wide operation now claims `path:<repo-root>` with `access: write`, and
conflict follows from one relation.

**Conflict.** Two claims conflict when they share a namespace, at least one has `access: write`, and
their keys are **equal or in an ancestor relation**:

```text
conflict(a, b)  ⟺  a.namespace == b.namespace
                   ∧ (a.access == "write" ∨ b.access == "write")
                   ∧ (a.key == b.key ∨ is_proper_ancestor(a.key, b.key)
                                     ∨ is_proper_ancestor(b.key, a.key))
```

For `path`, ancestry is on resolved path components: `/repo` is a proper ancestor of `/repo/plugins`,
and `/repo-a` is *not* an ancestor of `/repo-b` (component-wise, not string-prefix). For `external`,
ancestry is on `/`-separated segments of the declared name. Two reads never conflict, so
`read` on `<repo-root>` — a genuinely read-only global check — coexists with any number of other
readers and excludes only writers.

**This is a one-directional relation, so the asymmetry is gone by construction:** `path:/repo` write
conflicts with `path:/repo/b.py` write, and `path:/repo/b.py` write conflicts with `path:/repo` write,
because ancestry is tested both ways in the same predicate.

Derived claims, so a plan does not have to spell them out:

- Each declared output contributes `path:<the resolved output path itself>` with `access: write` —
  **the path, never its parent.** Revision 3 said `parent-or-self`, which on the parent reading makes
  every task that writes any file in one repository claim the repository root, so no two of them are
  ever concurrent and the design has no purpose. It also contradicted §8.2's own worked example, in
  which `path:/repo/b.py` is a claim a task can hold. A declared output that *is* a directory claims
  that directory, and ancestry then covers everything under it. Creating a file needs no claim on its
  parent directory: two workers creating two different entries in one directory do not race, and a
  task that rewrites a directory's contents wholesale declares the directory as its output.
- Each `depends_on` output contributes `path:<resolved path>` with `access: read`.
- A task whose declared **subject is a repository as a whole** — it commits, branches, rebases,
  fetches or otherwise changes index or ref state — contributes `path:<repo-root>/.git` with
  `access: write`, which is how index and branch state is protected: a claim on a path, not a special
  case. Editing a file that merely happens to live inside a repository is not such a task and
  contributes no `.git` claim; if it did, one claim per repository would serialize the whole plan.
- **Each resolved check contributes `path:<subject>` with `access: read` for every subject it names**
  (§11.2). This is what makes §11.6 coherent: a check the coordinator runs under the attempt's grant
  needs its subjects covered by that grant, and claims are never upgraded (§8.3), so they are claimed
  at derivation or not at all. A genuinely global check therefore contributes `path:<repo-root>` with
  `access: read`, which is exactly the claim that excludes a concurrent writer anywhere in that
  repository — the right answer, and the reason §11.1's axis is about *who runs* a check rather than
  about who holds its claim.
- `path:<project-dir>/project.json` with `access: write` is held by the coordinator, never granted to
  a worker.

**Normalization, so one claim set has one meaning.** Within a single set, duplicate keys are merged and
`write` subsumes `read`: a task that writes `/repo/a.py` and checks it holds one `write` claim on that
key, not a conflicting pair. Conflict is evaluated **only between distinct grants**, never inside one,
so an attempt is never in conflict with itself.

**A bare name is never a claim.** A plan that says `PROTECTED` or `the repository` is a derivation
error naming the offending value; every `path` claim is an absolute resolved path.

**Verification does not conflict with its own attempt.** A coordinator re-running a check for attempt
*A* runs **under A's existing grant**, after A's worker is known stopped (`result` published, or
`stop-evidence` written). It requests no new claim, so the self-conflict revision 3 had — where
global verification could not be scheduled because the attempt it was verifying held the paths — does
not arise. What it must not do is run a check for *A* while *B* holds a conflicting write claim; that
is an ordinary conflict and it waits.

### 8.3 Acquisition

- **Claims are computed up front, granted as one set, and never upgraded.** No `read`-to-`write`
  promotion, so the classic upgrade deadlock is unreachable rather than handled.
- **The grant set is inserted atomically** (§8.5), so partial acquisition never exists and there is
  no hold-and-wait state to order.
- A conflicting grant means the task **waits**; it never proceeds partially and never executes
  around the conflict.
- Waiting is bounded by the run's own deadline and reported. A task whose claims are held by an
  unresolved attempt of another project is reported with that attempt's id, because the fix is
  operator work, not a longer wait.

Deadlock is unreachable, not merely unlikely: with atomic all-or-nothing acquisition and no upgrades,
no executor ever holds one claim while waiting for another.

Two tasks in the same project that need overlapping writes are simply not concurrent. The scheduler
runs one, then the other, which is the correct answer — the plan says they share a subject.

### 8.4 Capacity

Concurrency is bounded by `min(host_capacity, configured_max, 4)`. Four is a **ceiling**, chosen
because §2 measured a 0.04x mean gain from raising it past four across all 31 projects while mean
maximum level width was 3.87. It is not a target and not an optimum, and a host that reports lower
capacity governs.

**The bound counts dispatched workers, not admitted tasks.** Inline execution on the coordinator does
not consume a worker slot — the coordinator is not a worker — but it does hold registry claims, so it
excludes conflicting work by the resource rule rather than by the capacity rule.

### 8.5 The workspace execution registry

Claims are enforced in a **workspace-scoped** registry, because concurrent projects in one workspace
routinely target the same repository, and a per-project store cannot see across that boundary:

```text
<workspace-root>/.execution-registry/
    registry.lock/                 # DirectoryLock, held only across scan-and-insert
    grants/<grant-id>.json         # one file per attempt, holding its entire claim set
```

A grant file carries `schema`, `grant_id`, `project_id`, `attempt_id`, `task_id`, `executor`
(`worker` or `inline`), `claims`, `acquired_at`, and `heartbeat_ref` (the attempt's heartbeat
directory, for staleness reporting).

**Acquisition:**

1. Take `registry.lock` with a short timeout.
2. Read every file in `grants/`. An unreadable grant file is a **conflict with everything** — it may
   name any claim — and is reported, not skipped.
3. Test the requested claim set against every existing set with §8.2's relation.
4. On no conflict, publish `grants/<grant-id>.json` (publish-if-absent, §7.1). One file, one atomic
   act, whole claim set.
5. Release the lock. Then write the attempt's `prepared.json`, which embeds `grant_id` and the
   baseline (§6.6).

The lock is held across a directory scan and one file creation — no subprocess, no commit, no host
call. It is a short mutex, not a long-lived ownership token, and §12.3's rule that no store lock is
held across a canonical commit is preserved.

**Release** is `release.json` in the journal **first**, then removal of the grant file. That order
makes recovery deterministic: a grant whose attempt has a `release` record is removable by any
coordinator, idempotently.

**A stale grant is not a removable grant.** A grant whose heartbeat is old and whose attempt has no
`release` is **reported, never reclaimed**. That is the difference between this design and a lease:
the registry has no way to know the process is dead, and reclaiming a live writer's claims is exactly
the corruption the registry exists to prevent. The operator path is: establish that execution stopped
(§13.2), write `stop-evidence` and a `disposition`, then `release`.

**Every executor acquires, with no exemption.** Inline execution on a coordinator, a sequential run at
capacity one, and a dispatched worker all take grants through the same path. A failure to acquire is
a **wait or a report** — never a licence to execute anyway. Revision 3's rule that a project could
proceed inline after failing to take a cross-project target lock is withdrawn: it permitted precisely
the conflicting concurrent write the mechanism exists to prevent.

**Registry bootstrapping.** The registry directory is created on first use with `exist_ok`, under no
lock, because `mkdir -p` of a directory tree is idempotent. `registry.lock` is a `DirectoryLock`
[C8], so its creation is the mutex.

**Cross-workspace targets are out of scope.** Two workspaces on one machine targeting one repository
are not coordinated by this design, and §21 says so rather than implying a guarantee.

## 9. Filesystem modes

| Mode | Description | Status |
|---|---|---|
| `shared` | All workers share the working directory; disjointness is enforced by §8.2 | **First mode.** Required for the two initial task shapes below |
| `copy` | Each worker gets a copy; results are merged by the coordinator | **Deferred.** Needs a merge protocol |
| `worktree` | Each worker gets a git worktree | **Deferred.** See below |

`shared` is enough for the two task shapes this protocol admits first:

- **Read-only analysis** whose result is one new file per worker. Every write is to a distinct new
  path, so disjointness is trivially satisfied.
- **Cooperative editing** where each worker owns disjoint declared paths. The claim relation of §8.2
  makes an overlap an admission failure rather than a race.

**Worktrees are deferred, and revision 3's "available, per project" claim is withdrawn.** A worktree
gives each worker an isolated checkout, which is attractive, and then requires an integration protocol
this design does not have: how a worker's commits reach the main working tree, what happens when two
worktrees touch one file, who resolves a conflict, whether the coordinator merges or rebases, what a
partial merge leaves behind on crash, and how `.git` claims interact when every worktree shares one
object database. Naming the mode available while owing all of that is the ambiguous promise this
revision is meant to remove. §21 records it as a scoped follow-on.

## 10. The worker contract

A worker receives its contract (§6.5) and is bound by it:

- **Write only inside your claims.** Every declared output, every capture, nothing else. A write
  outside the claim set is a protocol violation, detected at classification as
  `invalid_dependency` (§7.3) for paths the manifest names, and not detected at all for paths it does
  not — which is why claims are enforced by admission rather than by trust.
- **Never write canonical state.** Not `project.json`, not `evidence.md`, not `spec.md`, not
  `INDEX.md` [C5][C6].
- **Publish exactly one `result.json`**, last, after every capture and output it names (§7.2).
- **Heartbeat while working.** Advisory liveness only; a heartbeat never reports progress and nothing
  schedules on it (U6).
- **Run the checks you were given, as given.** Do not substitute, reinterpret, or add checks. A check
  that cannot be run is a `failed` result with `failure.kind: precondition`, not a silently different
  check.
- **Report refusal explicitly.** A worker that declines the work publishes `outcome: failed` with
  `failure.kind: refused` and a reason, rather than returning nothing.
- **Redact.** Secrets, credentials and tokens never enter a capture, a log, or a note (§5.7).

## 11. Verification

### 11.1 Three axes, decided at planning time

| Axis | Options | Rule |
|---|---|---|
| **Who executes the check** | worker, coordinator | Recorded per check as `executed_by` (§11.2). Default worker; `coordinator` when the check needs a claim the worker does not hold, or when independence is required. `independence: separate_actor` and `independence: human` force `executed_by: coordinator`, because a producer cannot be independent of itself |
| **Who judges the result** | always the coordinator | Never the worker. A worker's `outcome` is advisory (§7.4) |
| **Independence from the producer** | `none`, `separate_actor`, `human` | Set per check in the contract (§11.2), not inferred |

Independence exists because a task's verification may not be owned by a task that depends on it, and a
producer judging its own work is the same defect at a smaller scale. A check with
`independence: separate_actor` must be executed by an actor whose `relation_to_producer` is `separate`
(§11.3); `human` requires `relation_to_producer: human`. Adequacy (§11.5) enforces it.

### 11.2 Checks are resolved at planning time, not parsed at runtime

Canonical `verification` is free text. Runtime heuristics over free text are how a required command
becomes an inspection nobody ran, so the resolution happens **once, at planning, and is recorded**:

```json
"checks": [
  {"check_id": "pytest", "requirement": "the suite passes",
   "method": "command", "argv": ["uv", "run", "pytest", "-q"], "cwd": "<abs>",
   "subjects": ["<abs path>"], "independence": "none", "executed_by": "worker"},
  {"check_id": "ruff", "requirement": "lint is clean",
   "method": "command", "argv": ["uv", "run", "ruff", "check", "."], "cwd": "<abs>",
   "subjects": ["<abs path>"], "independence": "none", "executed_by": "worker"},
  {"check_id": "prose", "requirement": "§4 states the four properties",
   "method": "inspection", "subjects": ["<abs path>"], "independence": "separate_actor",
   "executed_by": "coordinator"}
]
```

`method ∈ {command, inspection, review}`. `argv` and `cwd` are required for `command` and forbidden
otherwise. `review` is `inspection` performed by a human and forces `independence: human`.
`executed_by ∈ {worker, coordinator}` is required, and a check whose `independence` is
`separate_actor` or `human` with `executed_by: worker` is a **derivation error**: the worker is the
producer, so it can never satisfy the independence the check asks for, and admitting the contract
would guarantee an adequacy failure later. `check_id` is unique within the contract, and a task with
several commands to run gets several checks rather than one composed one — that is what makes §11.5
able to say which one failed.

`checks_interpretation` records how the free text became this array: `rule_version` (an integer),
`source_text` (the canonical `verification` value verbatim), and `resolution` (one line per check
saying which fragment of the source it came from, and naming any fragment that produced no check).
The whole object is inside `definition_hash`, so a change to the interpretation invalidates the
contract (T25) instead of silently changing what "verified" means.

**Shell composition is rejected at admission, never downgraded.** A `verification` fragment containing
`&&`, `||`, `;`, `|`, backticks, `$(`, or a redirection cannot become a single `argv`, and the
admission error says exactly that, naming the fragment and the two supported options:

1. Split it into multiple checks, each with its own `argv` and `check_id` — the preferred form, since
   §11.5 then reports which one failed.
2. Declare it explicitly as `["bash", "-lc", "<the composed command>"]`, which is admitted only when
   the task's authorization covers running a shell (recorded in the check as
   `shell: true`), so the escalation is visible in the record rather than implicit in a string.

Revision 3 inferred `method: inspection` when tokenization failed. That silently replaced a required
executable check with a look at the file, which is the failure mode this section exists to prevent.

### 11.3 Capture records

One `capture.json` per execution of one check. The record is **tagged by kind**, so no field is
required for a shape it cannot have:

Common fields: `schema`, `capture_id`, `record_kind` (`command_run` or `assessment`), `check_id`,
`sequence`, `owner` (`worker` or `coordinator`), `attempt_id`, `actor`, `started_at`, `finished_at`,
`subject_digests_before`, `subject_digests_after`, `verdict` (`pass` or `fail`).

`actor` is `{kind, identity, relation_to_producer}` with `kind ∈ {agent, coordinator, human}` and
`relation_to_producer ∈ {self, separate, human}`. It is what §11.5 checks independence against, and it
is recorded rather than derived, because "who ran this" is not recoverable from a transcript later.

**`record_kind: command_run`** additionally requires `argv`, `cwd`, `exit_status`
(`{kind, code | signal}` with `kind ∈ {exited, signalled, timeout, not_run}`), `duration_ms`, and
`streams` (`{stdout: {path, bytes, truncated, digest}, stderr: {...}}`). It forbids `assessor_*`
fields. `exit_status.kind` is the honest generalisation of an exit code: a timed-out or signalled
command has no exit code, and revision 3's schema had nowhere to say so
[C15]. The pattern is the existing one — a launch error and a timeout are distinct
failures [C14][C15] — lifted into a record.

**`record_kind: assessment`** additionally requires `criteria` (the requirement text as judged),
`rationale` (bounded free text), and `assessor` (the identity that judged), and it forbids `argv`,
`cwd`, `exit_status`, `duration_ms` and `streams`. This is the record kind for `inspection` and
`review`, and it exists because revision 3's single command-shaped schema could not represent a check
that is a judgement — its required `argv` and `exit_code` had no meaning, so an inspection had to be
recorded as a fictional command.

**Digests bracket the check, and the record says what that proves.** `subject_digests_before` and
`subject_digests_after` are taken immediately before and after the execution. Equality shows the
subject did not change **across** the check, which is what matters for a check that must not mutate its
subject. It does **not** show the subject was unchanged *during* the check: a check that writes a file
and restores it looks identical. The record carries that sentence in its schema documentation, because
a guarantee readers assume is worse than one nobody claimed.

### 11.4 Re-running a check

The coordinator may re-run any `command` check. It runs under the attempt's existing grant, after the
worker is known stopped (§8.2), and it publishes a **new** capture directory under
`captures/coordinator/<check-id>-<seq>` with `owner: coordinator`. The worker's captures are never
modified — `result.json` is already sealed and names them — which is why the two owner subtrees exist.

A coordinator re-run does not change the classification and does not need a new `result`. It changes
which capture *qualifies* (§11.5), and the superseded one is retained in
`acceptance.superseded_captures`.

`inspection` and `review` checks cannot be re-run mechanically. A coordinator that disagrees with an
assessment publishes its own `assessment` capture with `owner: coordinator` and its own `actor`, and
adequacy then has two assessments for one `check_id` — the coordinator's qualifies, the worker's is
superseded, and both are preserved.

### 11.5 Per-check adequacy

Evidence adequacy is decided **per `check_id`**, not per attempt. For each check in the contract:

| Requirement | Rule |
|---|---|
| A capture exists | At least one `capture.json` for that `check_id` |
| Its kind matches the method | `command` needs `command_run`; `inspection` and `review` need `assessment` |
| Its subjects match | Every path in the check's `subjects` appears in `subject_digests_before` |
| It ran the right thing | For `command`, the capture's `argv` and `cwd` equal the check's |
| Independence is satisfied | `actor.relation_to_producer` is compatible with the check's `independence` |
| Streams are present | For `command_run`, both stream files exist and their digests match |
| It was run by the planned executor | A capture whose `owner` is `worker` never qualifies for a check with `executed_by: coordinator`; it is ignored, not superseded, because it was never asked for |
| It is the qualifying one | Among the captures that remain for this `check_id`, the one with the highest `sequence` for the highest-precedence owner (coordinator > worker) |

The **qualifying capture per check** is what `acceptance.qualifying_captures` names. Every other
capture considered for that check goes into `superseded_captures`, so a failed-then-passing history is
visible in the receipt rather than erased by it.

**Adequacy is about the record, not about the verdict.** A `valid_failed` result whose captures are
complete and well-formed is *adequate*: it is accepted and integrated as canonical `BLOCKED` with the
failure in evidence. A `valid_success` result missing a required check's capture is *inadequate*:
`hold/adequacy_failed` (T14). Conflating the two would mean a failing task could never be recorded,
which is the opposite of an audit trail.

**Adequacy is evaluated after §11.6, never before.** A check the worker was never asked to run has no
worker capture by design, so evaluating adequacy before the coordinator has run its own checks would
report `hold/adequacy_failed` for every independent check in every plan. The one absence adequacy does
report is a missing **human** assessment for a `review` check: the coordinator cannot produce it, T14
fires, and that is deliberate — a review nobody performed is an operator obligation, and quarantine is
how the protocol says so without inventing a status for waiting on a person.

When any qualifying capture has `verdict: fail`, `acceptance.canonical_intent` is `BLOCKED` regardless
of the worker's `outcome`. The coordinator reads captures, not claims.

### 11.6 The checks the coordinator runs itself

After classification and before adequacy, the coordinator executes every check of the contract whose
`executed_by` is `coordinator`:

- It runs **under the attempt's existing grant** and requests no new claim (§8.2), so an independent
  check never conflicts with the attempt it is checking. It runs only once the worker is known stopped
  — a published `result`, or `stop-evidence` — which is the same precondition as §11.4's re-run.
- Each execution publishes `captures/coordinator/<check-id>-<seq>` with `owner: coordinator` and an
  `actor` whose `relation_to_producer` is `separate`. `command` checks produce `command_run` records;
  `inspection` checks produce `assessment` records. Nothing is written into the worker's subtree.
- A `review` check is **not** run here. Its `independence` is `human`, no coordinator is a human, and
  a coordinator writing an assessment with `actor.kind: human` would be a forged record. It waits for
  a human assessment and reaches T14 if none exists.
- A coordinator-executed check that cannot run at all — the interpreter is missing, `cwd` does not
  exist — records `exit_status.kind: not_run` with `verdict: fail`. That is an adequate record of an
  inadequate environment, so the attempt is integrated as `BLOCKED` with the reason visible, rather
  than quarantined for a missing capture.
- This step is **idempotent across coordinator restarts** in the same sense as §11.4: a rerun
  publishes the next `<seq>`, the highest sequence qualifies, and the earlier one is superseded. A
  crash between two coordinator checks therefore has no special recovery rule — the next owner runs
  the checks with no qualifying capture and re-runs the rest at a higher sequence.

Revision 3 named the executor axis in §11.1 and then recorded it nowhere, ran it nowhere, and gave
adequacy no way to distinguish a capture that is missing from one that was never the worker's to
produce. `executed_by` and this section are that gap closed.

## 12. Coordinator ownership and fencing

### 12.1 Run identity and takeover

Each coordinator run has an opaque `coordinator_run`, recorded in `runtime.json` (for reporting) and
in canonical `execution.coordinator_run` (for fencing). Ownership is guarded by
`execution/coordinator.lock/`, a `DirectoryLock` [C8].

Takeover, when the lock's holder is stale:

1. Acquire `coordinator.lock` (or determine that its holder is unresponsive by the §13.2 bound).
2. **Commit a takeover**: one revision-checked canonical commit that sets `execution.coordinator_run`
   to the new run and increments `execution.ownership_generation`.
3. Rebuild `runtime.json` from the journal and the registry (§5.3).
4. **Complete every pending finalization** (§6.3) before anything else.
5. Treat every unresolved attempt of the predecessor as **claim-holding** (§5.2) and as possibly
   having a live writer. Resolve each through §13 before dispatching anything that conflicts with it.
6. If any `prepared` carries a `derivation_version` this coordinator does not implement, write
   `hold/derivation_version_changed` (T29) rather than reinterpreting the contract.

### 12.2 Fencing canonical commits

The fence is the canonical commit itself, and that is the entire mechanism:

- `ownership_generation` lives in `project.json` (§5.5), so raising it **consumes a revision**.
- A superseded coordinator's next commit carries a stale `--expected-revision` and is therefore
  **rejected by the existing transactional path**, not by a courtesy check it might skip.
- The superseded coordinator, on that rejection, reloads, sees a generation higher than its own, and
  stops rather than reconciling — a reload-and-retry would be a superseded writer re-entering the
  race.

Revision 3 checked ownership and then wrote, with a window between the two. Here there is no window,
because the check *is* the write: two coordinators racing to commit at the same revision means exactly
one succeeds, by the same mechanism that already protects every other canonical change.

### 12.3 Fencing physical dispatch

A revision check cannot fence a `spawn` call, because spawning is not a commit. Three rules together
bound the damage:

1. **`start-permit` records the generation** at which its canonical `RUNNING` commit landed, and the
   coordinator **re-reads canonical `ownership_generation` immediately before the host call**. A
   mismatch aborts the dispatch and writes `uncertainty/dispatch_interrupted` (T5). This narrows the
   window to the interval between one read and one call; it does not eliminate it, and this document
   says so rather than claiming a fence it does not have.
2. **A superseded coordinator cannot integrate what it launched.** Its acceptance commit is rejected
   (§12.2), so a stale worker's result can only ever be integrated by the *current* owner, which will
   classify it against the contract it finds in the journal.
3. **A stale launch lands inside its own still-held reservation.** Takeover treats the predecessor's
   unresolved attempts as claim-holding (§12.1 step 5), so the successor cannot grant a conflicting
   claim to a new worker. The worst case is wasted work by a doomed attempt, not two writers on one
   path.

Lock order, wherever more than one is taken — and no lock is ever held across a canonical commit or a
host call:

```text
1. <workspace-root>/.execution-registry/registry.lock/   (scan and insert, §8.5)
2. <project-dir>/execution/coordinator.lock/             (ownership, §12.1)
3. <project-dir>/.project.lock                           (canonical commit and evidence append [C16])
4. <workspace-root>/.index.lock                          (index regeneration)
```

## 13. Recovery

Recovery reads the journal, the registry and canonical state, and takes no action that a record does
not justify. Every step below is idempotent.

1. Acquire ownership and commit a takeover (§12.1).
2. Rebuild `runtime.json` (§5.3). Nothing that follows depends on its previous contents.
3. Complete every pending finalization (§6.3). This is what frees capacity a dead run held.
4. Label every attempt (§6.1). An unmatched record set is reported, never labelled.
5. Reconcile canonical state against the journal:
   - A task canonically `RUNNING` whose attempt in `execution.attempts` has no `start-permit`: no host
     call happened; commit `RUNNING → TODO` and release.
   - A task canonically `RUNNING` with an attempt labelled `UNCERTAIN`: leave it `RUNNING`. It is the
     honest status — something may still be executing — and §13.2 governs what happens next.
   - A task canonically `TODO` with an attempt holding claims: finalize per §6.3.
   - A `PUBLISHED` attempt: classify it (§7.3). This is normal completion of interrupted work, not an
     error.
   - A `VERIFIED` attempt: apply §6.3's accept-and-commit prefix rule.
6. Remove every grant whose attempt has a `release` record. Report every other grant whose attempt is
   unresolved, including stale ones (§8.5).
7. Recompute `dispatch_blocked` (§7.3) and only then consider admission.

### 13.1 Recovery with a published failure

Worth stating on its own, because revision 3 got it wrong. A run that crashed after a worker published
`outcome: failed` and before the coordinator classified it leaves: a `result`, no `classification`, no
hold, no flag anywhere. `dispatch_blocked` is nonetheless **true**, by its first clause — a result with
no classification — so the restarted coordinator does not dispatch new work. It classifies the failure
(`valid_failed`, T8), checks adequacy, and integrates it as canonical `BLOCKED`, at which point
`dispatch_blocked` becomes false through the ordinary path.

The reason this works is that the pause is a query over records rather than a flag written after the
fact. There is no ordering to get wrong and nothing to lose in a crash.

### 13.2 Lost contact, and what counts as stopped

Contact is lost when `now − last_heartbeat > heartbeat_bound_ms` or the contract deadline passes
with no result, where

```text
heartbeat_bound_ms = max(90000, 3 × contract.heartbeat_interval_ms)
```

`heartbeat_interval_ms` is required in every contract (§6.5), so there is no absent-interval case
to give a meaning to. Before the first heartbeat, `last_heartbeat` is the `launch` record's
timestamp: a worker that never beats at all is still bounded. The coordinator writes `uncertainty/worker_gone` or
`uncertainty/deadline` (T17, T18) and the attempt is `UNCERTAIN`. It **retains its claims**.

`UNCERTAIN` means *we do not know whether a writer is still running*. It is therefore never resolved
by a timeout. `stop-evidence` requires one of:

| Evidence kind | What establishes it |
|---|---|
| `adapter_finished` | `discover` returned `finished` for the handle or launch key (§17) |
| `adapter_never_started` | `discover` returned `never_started` |
| `process_absent` | The recorded process identity does not exist, and its identity included enough to exclude reuse (start time, or a host-provided token) |
| `operator_attested` | A human recorded that execution stopped, with what they checked |
| `host_terminated` | The adapter reported a successful `interrupt` and then `finished` |

**A bare pid is not evidence.** Pids are reused, and `process_absent` requires an identity that
excludes reuse. **`unknown` from `discover` is not evidence either** — it is the adapter saying it
cannot tell, and the correct outcome is a retained `uncertainty` and an operator report, not a
release.

Only after `stop-evidence` may a `disposition` be written and the grant released. That is the concrete
fix for revision 3's defect where a quarantined or uncertain attempt was terminal for scheduling while
possibly still holding a live writer.

### 13.3 Reconciling partial outputs

A stopped attempt that wrote some of its declared outputs is reconciled from the record, not from a
guess:

1. Compare each declared output's current digest against `baseline.writable_subjects` (§6.6). Three
   outcomes per subject: unchanged (`state: present`, digest equal), created (`state: absent`, now
   present), or modified (digest differs).
2. Write `disposition` with resolution `accept_partial` (T21) or `block` (T20). `accept_partial`
   records the per-subject comparison as evidence and sets the task `RUNNING → BLOCKED` — never `DONE`,
   because a partial output has not satisfied the success criteria.
3. Deleting or reverting a partially written output is **not** done automatically. The protocol has no
   general undo, and inventing one would destroy work whose value only the operator can judge. The
   disposition records what exists; the operator decides.
4. Then `release`.

## 14. Failure semantics

| Failure | Detection | Response |
|---|---|---|
| Worker crash before publishing | Heartbeat bound | `uncertainty/worker_gone` → §13.2 |
| Worker publishes a failure | `classification: valid_failed` | Adequacy, then canonical `BLOCKED` with evidence |
| Worker writes outside its claims (named) | Classification | `hold/invalid_dependency` |
| Worker writes outside its claims (unnamed) | **Not detected** | Prevented by admission, not by detection (§10) |
| Unreadable record | Any read | `hold/unreadable_record`; never treated as absent (§5.5) |
| Conflicting publication | Publish-if-absent | `hold/conflicting_publication` (§7.1) |
| Prerequisite changed under an attempt | Baseline recheck | `hold/invalid_dependency` |
| Plan changed under an attempt | `definition_hash` recompute | `hold/contract_invalidated` (T25) |
| Coordinator crash | Next start | §13 |
| Coordinator superseded | Rejected `--expected-revision` | Stop; do not reconcile (§12.2) |
| Registry unreadable grant | Acquisition scan | Conflict with everything; report (§8.5) |
| Host adapter unavailable | `spawn` or `discover` error | Fall back to inline execution at capacity one; still take a grant |
| Authorization withdrawn | Re-check before dispatch and at each commit | §14.1 |
| Deadline exceeded | Wall clock | `uncertainty/deadline` → §13.2 |

**Consecutive-failure bound.** After `max_consecutive_failures` (default 3) attempts reach a hold
without an intervening acceptance, dispatch stops and the run reports. What the bound bounds is
*automatic re-dispatch* — it does not stop classification, reconciliation, finalization or reporting,
all of which must run in order to clear it.

### 14.1 Stop requests are not execution failures, and withdrawal is coherent

Two distinct things were conflated in revision 3, and separating them is a correctness fix, not a
presentation one.

**A stop request** is `hold/stop_requested`: an operator asked for the work to stop. It counts toward
nothing, is not a failure, and its disposition is ordinarily `retry` (T22) or `block` (T20).

**Authorization withdrawal** is `hold/authorization_withdrawn`, and its handling is ordered so that no
step erases the previous one:

1. **Write `hold/authorization_withdrawn` first.** It is a journal record, so it cannot be lost by a
   later crash and cannot be overwritten. `dispatch_blocked` becomes true immediately (§7.3).
2. **Best-effort `interrupt`** through the adapter (§17).
3. **Establish that execution stopped** (§13.2) — `stop-evidence`, or a retained `uncertainty` and an
   operator report. The attempt keeps its claims throughout.
4. **One canonical commit** sets the task `RUNNING → BLOCKED` **and** `authorization.status` to
   `denied` together. Both are legal in one candidate: `RUNNING → BLOCKED` is in `TASK_TRANSITIONS`
   [C20], `denied` is in `AUTHORIZATION_STATUSES` [C17], and validation's explicit-authorization rule
   fires only for `RUNNING` and `DONE` tasks [C19] — so the task is no longer `RUNNING` at the moment
   the status becomes `denied`, and the commit validates.
5. Write `disposition` (`block`), then `release`.

Splitting step 4 into two commits would be the incoherence the review identified: a commit that sets
`denied` while the task is still `RUNNING` is rejected by validation, and a commit that blocks the task
first and records the withdrawal second leaves a window in which the record does not say why. One
commit avoids both, and the `hold` record written in step 1 means the reason survives even if the
commit never happens.

**Re-authorization is a new attempt.** A task blocked this way returns to `TODO` only through a fresh
authorization and a new contract, because the frozen scope the old attempt held is no longer the one in
force (§8.1).

## 15. Timing and observability

| Quantity | Default | Note |
|---|---|---|
| Heartbeat interval | 30s | Advisory (U6) |
| Heartbeat bound | 3 × interval, min 90s | Produces `UNCERTAIN`, never a release |
| Task deadline | Per contract | Produces `UNCERTAIN`, never a release |
| Registry lock timeout | 10s | Scan-and-insert only (§8.5) |
| Coordinator lock timeout | 30s | Ownership |
| Project lock timeout | 30s | As today [C8] |
| `max_consecutive_failures` | 3 | Bounds automatic re-dispatch only (§14) |
| `max_log_bytes` | 262144 per stream | Truncation recorded in the capture (§5.7) |

Every timing value above is a **default, configurable per project**, and none of them is a correctness
mechanism. Nothing in this protocol releases a claim, resolves an uncertainty, or reaches a terminal
state because time passed.

Reporting reads the journal and the registry. A report states, per attempt: its label, whether it holds
claims, its unresolved holds and uncertainties, and its qualifying captures per check. A report that
would need `runtime.json` to be current is a report that can be wrong; §5.3 is why it never does.

## 16. Authoring plans that can actually run in parallel

§2 measured this workspace's plans as chain-shaped, so authoring guidance is part of the deliverable.

- **Separate independent subjects into separate tasks.** Three independent files analysed in one task
  is one task; as three tasks it is three. This is the single largest lever, and it is free.
- **Depend on what you read, not on what ran before you.** A `depends_on` edge added for narrative
  order is a serialization with no cause.
- **Keep effect-bearing work in its own task.** A task that is 90% analysis and 10% destructive edit
  is entirely inline (§8.1); split, and the analysis delegates.
- **Do not fragment for width.** A plan split past the point where each task has a coherent subject
  raises average level width and lowers quality, and the model in §2 would still show a gain — which
  is exactly why the metric is not the goal (§1).
- **Declare disjoint outputs.** Overlapping writes are an admission failure (§8.1), so a plan that
  intends concurrency must declare paths that do not overlap.
- **A chain is a valid answer.** The project that produced this document is width 1.0 throughout and
  correctly so.

## 17. Host adapter contracts

The protocol runs on a host that provides subagents. Capabilities differ, and the adapter states them
rather than the protocol assuming them.

| Capability | Meaning | If absent |
|---|---|---|
| `spawn` | Start a worker with a prompt and a working directory | No parallel execution; inline at capacity one |
| `discover` | Report on an attempt or launch key | Every dispatch ambiguity becomes a retained `uncertainty` (§13.2) |
| `interrupt` | Request termination of a handle | Withdrawal cannot stop a worker; §14.1 step 3 needs an operator |
| `bounded_context` | Worker output does not enter coordinator context | The context-economy benefit of §1 is unavailable and the report says so |
| `atomic_link` | `link(2)` is atomic and returns `EEXIST` | **Refused for parallel execution** (§7.1) |

Two operations have contracts, because §6.3's determinism depends on them:

```text
spawn(contract) → started(handle)               a worker is running
                | definitively_not_started      nothing was started; safe to retry (T4)
                | ambiguous                     unknown; T5, and no re-dispatch without evidence

discover(handle | launch_key) → running(handle)  still executing
                              | finished        execution ended (outcome unknown to the adapter)
                              | never_started   nothing ever ran
                              | unknown         the adapter cannot tell — not evidence (§13.2)
```

An adapter that cannot distinguish `definitively_not_started` from `ambiguous` must return `ambiguous`.
Reporting a definite answer it does not have is what converts a recoverable window into a double
launch.

**The inline adapter** is the trivial implementation and its guarantees are stated, not assumed:
`spawn` runs the work synchronously in the coordinator process and therefore returns `started` or
`definitively_not_started` and **never** `ambiguous`; `discover` always returns `finished` after the
call returns; `interrupt` is unavailable; `bounded_context` is false. Because it never returns
`ambiguous`, an inline run has no dispatch-ambiguity window at all — which is why inline is the
fallback for every degraded host (§14).

Claude Code provides `spawn` and `bounded_context`. `discover` and `interrupt` are **not currently
available**, so on this host every dispatch ambiguity resolves to a retained `uncertainty` needing an
operator, and withdrawal cannot stop a running worker. That is a limitation of the host, recorded here
so no rule silently depends on a capability this host lacks.

## 18. Unenforced rules

Rules a coordinator must follow that nothing checks. Each is marked **[UNENFORCED]** where it appears.

| # | Rule | Why unenforced |
|---|---|---|
| U1 | Only the coordinator writes canonical state | No filesystem ACL separates them |
| U2 | A worker writes only inside its claims | Detected only for paths its manifest names (§14) |
| U3 | A worker never edits another attempt's journal records | Same directory tree, same uid |
| U4 | Verification is judged by the coordinator | A worker could fabricate a capture |
| U5 | Claims are computed from declared outputs | An undeclared write is invisible to derivation |
| U6 | Heartbeats never carry progress | Nothing validates heartbeat content |
| U7 | No store lock is held across a canonical commit | No mechanism enforces lock discipline |
| U8 | Attempt ids are never reused | Only convention and the id syntax (§5.4) |
| U9 | Captures are redacted before writing | No scanner inspects capture bytes |
| U10 | Collection tombstones precede deletion | Only the coordinator's own ordering |
| U11 | An unreadable record is never treated as absent | A reader could silently skip it |
| U12 | A stale grant is reported, never reclaimed | Nothing prevents a coordinator deleting a grant file |

U11 and U12 are new in revision 4 and are the two that most directly protect against double execution.

## 19. Implementation stages

Each stage is independently valuable and independently revertible. Stage 7 is the only one that
changes observable behaviour.

| Stage | Content | Verified by |
|---|---|---|
| 1 | The journal store: publish-if-absent (§7.1), record schemas, label derivation (§6.1) | Model tests: every label, every crash prefix |
| 2 | The measurement script and graph fixtures behind §2 | Reproduce the table in CI |
| 3 | The registry (§8.5): conflict relation, atomic grant insert, report-never-reclaim | Model tests: conflict matrix including ancestry and the global-versus-scoped case |
| 4 | Readers understand `schema_version` 4; transitive evidence-reference validation (§5.7) | Existing validator tests plus new v4 fixtures |
| 5 | Derivation (§6.6): three identity sets, check resolution (§11.2), admission (§8.1) | Model tests: admission matrix over the real enums |
| 6 | `enable-execution` (§5.5); recovery and finalization (§6.3, §13) | Migration tests; mixed-version refusal on all three hosts |
| 7 | Host adapters (§17) and dispatch | End-to-end on a plan with genuine width |
| 8 | Reporting (§15) and the benchmark of §20.3 | The benchmark runs |

Rollout order inside this table is a correctness requirement for stages 4→6→7 (§5.5), not a
preference.

## 20. What is verified, and what is not

### 20.1 The documentation checks

`tests/plugins/research/test_parallel_execution_doc.py` checks **relationships in this document**, and
each check is written so that breaking the relationship fails it:

| Check | What it verifies |
|---|---|
| Citation integrity | Every `[Cn]` used has a §22 row; every row is used; ids are unique and contiguous; **every cited line range is re-read from the named source and must contain the row's needle** |
| Link resolution | Every Markdown link target resolves on disk from this file's directory |
| Section references | Every `§N` and `§N.M` mentioned in the text has a corresponding heading |
| Canonical enum agreement | Every canonical constant, status, effect kind and authorization status named here is read out of `workspace_lib.py` and must exist there; §8.1's `delegable` names exactly the two workspace-confined effect kinds; and no word that merely *reads* like an authorization status — revoked, granted, expired — is written as one |
| Canonical transition validity | Every canonical transition in §6.2 is permitted by `TASK_TRANSITIONS`, read from source |
| Label classification | The labels in §6.1's table, the count claimed in its prose, and the labels used in §6.2 are mutually consistent, with no label used before it is defined |
| Transition completeness | Row ids in §6.2 are unique and contiguous from T1, and every `T<n>` mentioned outside that table resolves to a row |
| Unenforced completeness | Row ids in §18 are unique and contiguous from U1, and every `U<n>` mentioned outside that table resolves to a row |
| Retired components | Withdrawn mechanisms appear only in the history sections that name them as withdrawn |
| Benchmark honesty | The §2 disclaimer is present until the fixtures land, and its retirement is a single stated edit |
| Disposition completeness | The decision record carries a row for every finding id of every review file present, matched per review section rather than anywhere in the file |
| Companion cross-links | This document names the decision record, the decision record names this document, and the decision record says which of the two is normative |
| Section inventory | Every section either document is cross-referenced by is present under the name used to reference it |

Each check is paired with at least one **mutation**: a substitution on a copy of the document text that
must make exactly that check fail. The checks are functions of `(reference text, decision text,
repository root)` for this reason, so the same code runs against the real documents and against mutated
copies. A check that cannot be made to fail proves nothing, and review 03 found three such checks by
mutating the documents by hand; those three mutations — deleting C1's row while `[C1]` is still
cited, changing a canonical cell to a transition `TASK_TRANSITIONS` forbids, and repointing the
companion link at a path that does not exist — are now named cases in the suite.

The constants are parsed out of `workspace_lib.py` with `ast` rather than imported, because
`pyproject.toml` measures coverage of the shipped scripts package and a prose checker has no business
contributing to that measurement.

These are **documentation checks**. A green run means this document is internally consistent, its
citations are real, and its state machine agrees with the canonical constants. It says nothing about
whether the protocol is correct.

### 20.2 The model tests

`tests/plugins/research/test_parallel_execution_model.py` is an **executable model of the protocol's
decision functions**, at design stage, with no production executor and nothing wired into
`research:project`. It implements, in the test file itself: the label function and finalization
predicate of §6.1, §6.2's transition table as a reachability graph, the claim-conflict relation of
§8.2, the delegability and in-force predicates of §8.1, the `dispatch_blocked` predicate of §7.3, the
crash-prefix recovery outcomes of §6.3, and the capture-adequacy and qualifying-capture selection of
§11.5 and §11.6.

The graph is the part worth explaining. §6.2 declares each row's source as a *label*, and §6.1 computes
the label from a record set; the two can disagree. So the model fires a row only when `label()` applied
to the predecessor actually returns one of that row's declared sources — never because the table says
so. Two failure modes then become mechanically visible: a **dead row**, whose declared source the label
function never produces, and an **unlabelled reachable state**, which is a record set §6.2 can produce
and §6.1 cannot read.

What it asserts, by group:

- **The table is a bijection.** Fourteen rows, fourteen labels, ids contiguous from 1; each row's
  positively required records are individually necessary (removing one must stop the row matching);
  wherever two rows match, the answer is the earlier row.
- **Labels and finalization are orthogonal.** `finalized ⟺ release`, `holds_claims ⟺ prepared ∧
  ¬finalized`; adding `release` to a finalizable attempt does not change its label; `{prepared,
  release}` is unreachable and is reported rather than labelled.
- **Ordering consequences.** `uncertainty` outranks every other row over an exhaustive enumeration of
  small record sets; a hold beats rows 8–13; no hold source is finalizable.
- **Reachability (§6.2).** Every reachable state has a label; no row is dead; a `disposition` edge's
  source label is always `QUARANTINED` or `STOPPED`; a hold always lands as `QUARANTINED` or stays
  `UNCERTAIN`; `release` appears only under a finalizable label; no path reaches `UNCERTAIN`,
  `QUARANTINED` or `STOPPED` with the grant already dropped; the states with no successor are exactly
  the four finalized ones; and the record pairs §6.1's ordering never has to rank are derived to be
  unreachable rather than assumed.
- **Liveness of the dispatch gate.** From every reachable state that blocks dispatch, some
  continuation admits it again.
- **Claims and admission.** Conflict is symmetric, reflexive on writes, ancestor-sensitive in both
  directions, and false for read/read and for sibling paths sharing a string prefix; a bare name is a
  derivation error; verification requests nothing under its own grant. Delegability and in-force are
  checked against `EFFECT_KINDS` and `AUTHORIZATION_STATUSES` read out of `workspace_lib.py`, reject
  `pending`, `denied` and `deferred`, and represent withdrawal as one status change.
- **Checks and captures.** A `separate_actor` or `human` check with `executed_by: worker` is a
  derivation error for every method; a worker capture never satisfies a coordinator-executed check;
  adequacy evaluated before §11.6 fails a plan that is in fact fine; a `review` check needs a human
  assessment; a capture never carries both kinds' fields; a coordinator re-run supersedes a failing
  worker capture and the highest sequence wins within an owner, independently of the order captures
  are considered in; several commands are judged separately, so a failing one names itself; and a
  `not_run` command is an adequate record of a `BLOCKED` task rather than a quarantined attempt.
- **Recovery.** Every prefix of every multi-file transition in §6.3 maps to exactly one outcome;
  applying it twice equals applying it once; exactly one dispatch prefix is resolved by evidence
  rather than by rule; and no outcome redispatches.

**This model changed the document.** Six defects in revision 3 were found by running it, not by
reading: rows 1–3 of §6.1 required `release`, so three of the four finalization-pending record sets
matched no row at all; row 1 omitted the `retry` resolution that T22 writes; §7.3's first clause
deadlocked permanently on a quarantined publication; T28 and T29 permitted a hold from `INTEGRATED`\*
and `LAUNCH_FAILED`\*, where rows 4 and 7 shadow it; and §6.1's `prepared` precondition was implicit,
so `{launch}` alone silently labelled `DISPATCHED`; and adequacy had no way to tell a capture that is
missing from one the worker was never asked to produce, so every independent check in every plan would
have quarantined its attempt. Each fix is in the section named, and each is covered by the assertion
that failed.

Three further defects were found by **walking the ten scenarios of the revision brief** rather than by
either test, and are recorded in the decision record: §8.2's `parent-or-self` output claim, which on
the parent reading serializes every task writing anywhere in one repository; §11.1's executor axis,
named and then recorded nowhere and run nowhere; and check subjects contributing no claim, which left
§11.6 needing a claim §8.3 forbids acquiring late. A model finds what it models; a walkthrough finds
what nobody thought to model.

**What the model does not establish.** It is a model of the decision functions as *specified*, so it
cannot find a defect that this document and the model share, and it does not exercise real
filesystems, real concurrency, real hosts, or real timing. A production implementation must be checked
against the world, not against this model. **Claim derivation is not modelled at all**: the
model takes claim sets as given and checks the conflict relation over them, so §8.2's rules for turning
outputs, dependencies and check subjects into claims are enforced only by review and by the
walkthrough — which is how the `parent-or-self` defect survived three revisions of a green suite. Its reachability graph is also a *model* of §6.2, written
by hand from the table: a row transcribed wrongly into the graph would be checked consistently and
still be wrong, which is why the documentation checks re-read §6.2's canonical column from
`workspace_lib.py` independently.

### 20.3 The benchmark that would settle §1

One project, run three ways — sequential, parallel with bounded worker context, parallel without —
recording wall-clock time, coordinator tokens, worker tokens, total cost, commit count, and a quality
verdict against the plan's own success criteria. Until it runs, §1's context-economy claim stays a
hypothesis and §2's speedup figures stay optimistic estimates.

## 21. Deferred and out of scope

Each item below is named rather than implied, so no reader mistakes silence for a promise.

| Item | Why deferred | What it would need |
|---|---|---|
| `worktree` mode | No integration protocol (§9) | Merge or rebase rules, conflict ownership, `.git` claim semantics across worktrees, partial-merge recovery |
| `copy` mode | No merge protocol | Per-subject merge rules and a conflict record |
| Directory and external artifact hashing | No canonical serialization for a tree (§6.6) | A defined tree digest, ordering and metadata policy |
| Hardlink aliasing detection | Two claims may name one inode (§6.6) | Inode identity in the claim relation, and a policy for cross-device claims |
| Cross-workspace coordination | The registry is workspace-scoped (§8.5) | A machine-scoped registry, and a way to discover other workspaces |
| A durable store other than the filesystem | The journal's obligations are retention and recovery, not a schema (§5.6) | The same durability classes, the same publish-if-absent semantics, and a migration |
| Worker-side dependency resolution | The coordinator assigns (§4) | A distributed claim protocol, which this design does not need |
| Progress reporting from workers | Heartbeats are advisory (U6) | A progress schema and a rule for what schedules on it — currently nothing does |

## 22. Citations

Every row is re-read by the documentation test: the file is opened, the line range is sliced, and the
needle must appear inside it. A row whose needle has moved fails the test rather than aging quietly.

| Id | Source | Lines | Needle |
|---|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1345-1352 | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2768-2789 | `def _dependency_levels(` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2791-2815 | `def build_task_graph(` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2685-2692 | `def levels(` |
| C5 | `plugins/research/skills/project/SKILL.md` | 180-184 | `One coordinator owns writes to` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md` | 11-14 | `One coordinator is the sole writer` |
| C7 | `plugins/research/skills/project/SKILL.md` | 384-385 | `Use a dependency graph only when independent work can run in parallel` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py` | 319-352 | `class DirectoryLock` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py` | 296-312 | `def atomic_write_text(` |
| C10 | `plugins/research/skills/project/scripts/workspace_lib.py` | 28 | `EFFECT_KINDS = {` |
| C11 | `plugins/research/skills/project/scripts/workspace_lib.py` | 35 | `REFERENCE_ROOTS = {` |
| C12 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2244-2252 | `unsupported schema_version` |
| C13 | `plugins/research/skills/project/scripts/workspace_lib.py` | 457-470 | `def _validate_evidence_reference(` |
| C14 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2543-2550 | `completed = subprocess.run(` |
| C15 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2551-2556 | `command timed out after` |
| C16 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2584-2596 | `must not see a half-written one` |
| C17 | `plugins/research/skills/project/scripts/workspace_lib.py` | 29 | `AUTHORIZATION_STATUSES = {` |
| C18 | `plugins/research/skills/project/scripts/workspace_lib.py` | 539-541 | `non-required authorization must use status` |
| C19 | `plugins/research/skills/project/scripts/workspace_lib.py` | 551-553 | `task requires explicit authorization` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py` | 59-65 | `TASK_TRANSITIONS = {` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3114-3115 | `IMMUTABLE_PROJECT_FIELDS` |
