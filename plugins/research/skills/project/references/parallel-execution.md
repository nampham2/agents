# Parallel task execution

> **Status: design awaiting implementation.** Nothing in this repository implements this protocol.
> No script, schema field, MCP server, or `SKILL.md` step exists for it, and this document is not
> linked from `SKILL.md` precisely so that no coordinator is told to follow a protocol the tooling
> cannot execute. It is a specification for a successor project to build against, and a record of
> the decisions and measurements behind it. Until that project ships, `research:project` executes
> tasks sequentially and delegation is out of policy.
>
> **Revision 2, 2026-09-11.** Rewritten after an external expert review of revision 1
> (`e18e589`), recorded at `docs/parallel-execution-review.md`. The review found seven high-priority
> problems and voided the measurement on which revision 1's central architectural choice rested.
> Revision 1 specified a SQLite work queue that workers pulled from through a hand-written MCP
> server; this revision specifies coordinator-assigned attempts and a durable file-based result
> inbox, and defers SQLite and MCP to a final stage admitted only against a measured need. §20
> gives the disposition of every finding, including the one answered rather than adopted.

## 1. What this designs

A protocol for executing the tasks of one `research:project` plan **concurrently**: independent tasks
dispatched by one coordinator to Claude Code subagents, with results returned through a durable inbox
and integrated into canonical state by that same coordinator, which remains the sole writer of it.

It is a protocol, not an engine. The machinery it needs mostly exists already; §3 says exactly what.

The objective is **lower completion time and lower coordinator context consumption, at preserved
quality and controlled total cost.** Two things follow that are easy to get wrong:

- **Wider plans are a technique, not the goal.** §2 shows this workspace's plans are shaped like
  chains, so guidance on authoring is part of the deliverable (§14) — but a plan fragmented to raise
  average level width trades one coherent artifact for a metric, and increases assembly and review
  work while the number goes up.
- **Context economy is a hypothesis with a benchmark attached, not an established benefit.** A
  worker's tool output never enters the coordinator's context window, and on a long project that is
  plausibly the larger win. Nobody has measured it. §19 says what would settle it, and until that
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
is optimal, and this document does not claim it is. The script and graph fixtures behind the table
ship with the implementation (§19); a figure whose derivation is not reproducible is an assertion.

Three consequences shape everything below.

- **Wall-clock speedup is not the justification.** A design sold on 1.45x, measured optimistically,
  against real overheads it does not count, would not survive contact with a real project.
- **Four is a cheap ceiling.** It is within 0.04x of unlimited across all 31 projects, and mean
  maximum level width is 3.87, so a higher bound buys nothing this sample can see. §7.3 states it as
  a ceiling with that reason, not as a required worker count and not as an optimum.
- **The bottleneck is the plan, not the executor.** This is why §14 exists.

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
  useful for *presentation*; §7 schedules on satisfied dependencies rather than on level barriers.
- **The ownership model is already normative.** One coordinator owns writes to `project.json`, shared
  records and `INDEX.md`; workers may write only assigned non-overlapping output paths and must not
  edit canonical state [C5], stated identically in the schema reference [C6]. The planner is already
  told to use a dependency graph only where independent work can run in parallel, to assign
  non-overlapping outputs, and to keep one canonical writer [C7].
- **Cross-process safety already exists.** A `mkdir`-based `DirectoryLock` [C8] plus
  `commit --expected-revision` gives optimistic concurrency: two writers means one loses, reloads,
  and reconciles.

So five things are missing, and they are what this document supplies: a **scheduler**, a **worker
contract**, a **durable result-and-evidence integration protocol with a recovery contract**, a
**resource model**, and a **host-capability contract**.

### 3.1 What the locks actually do — and what revision 1 got wrong

Revision 1 built its architecture on a claim that a committing coordinator and evidence-recording
workers would contend on `.project.lock` for the duration of an index rebuild, against a 5 s timeout
the CLI cannot raise. **That claim is false in both halves, and it is recorded here because it was
the load-bearing argument for the component this revision deletes.**

- `record_evidence` runs the verification command *before* taking any lock [C9]. The lock is acquired
  only by `_append_evidence_entry`, and it covers a read-modify-write of `evidence.md` — deliberately
  the project lock rather than one of its own, so a commit never validates a half-written evidence
  file [C10].
- `commit_candidate` *releases* `.project.lock` before rebuilding the index, on purpose: a failed
  rebuild must not read as a failed commit [C11].

So the critical section is short in both paths, and no measurement of its duration or frequency was
ever taken. Contention remains possible under concurrency and is worth measuring (§19) — but it
justifies nothing on its own, and it certainly does not justify a worker-pulled database behind a
hand-written server. **Coordinator-only evidence recording is retained in this revision for a
different and sound reason: one writer on shared evidence is the same rule as one writer on canonical
state, and it needs no lock measurement to stand.**

The generalizable lesson, and the reason this subsection is not deleted along with the claim: an
architecture derived from a measurement is only as sound as the measurement, and "I read the code and
it takes the lock" is not the same fact as "it takes the lock around the expensive part". §16 records
the same failure mode in its other direction.

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
                     ▲ dispatch (down)      ▲ result (up, atomic)
                     │                      │
              ┌──────┴─────┐  ┌────────────┐  ┌────────────┐
              │  worker 1  │  │  worker 2  │  │  worker N  │   N ≤ 4 (§7.3)
              └────────────┘  └────────────┘  └────────────┘
                 filesystem mode chosen per §8, not assumed shared
```

Three properties define it, and each one is a reversal of revision 1:

1. **The coordinator assigns; workers do not claim.** At four workers the coordinator already owns
   every decision before execution (eligibility, authorization, resources, capacity) and after it
   (verification, evidence, commit). A worker discovering its own next task adds a distributed
   protocol to a problem that has none.
2. **The middle layer is durable, not derived.** Its justification is *recovery*, not lock
   avoidance: a coordinator that dies holding an uncommitted result must not lose completed work.
   That means the store's contents are classified (§5.3) and its records are written atomically.
3. **There is one scheduler.** Sequential execution is this scheduler at capacity one — not a second
   mechanism. A separate wave-synchronous path would be the code that runs on every degraded host and
   is therefore exercised least, which is how a fallback rots.

## 5. The execution store

### 5.1 Layout

```text
<project-dir>/execution/
    attempts/<attempt-id>/
        dispatch.json          # coordinator-written, immutable once released
        result.json            # written by the assigned writer, atomically
        verification.json      # capture receipt(s) for this attempt
        stdout.log
        stderr.log
    receipts/<content-hash>.json   # immutable integration receipts
    runtime.json               # coordinator bookkeeping
```

Inside the project directory, matching how every existing lock and file is scoped: it keeps a
project's execution record inside the directory that *is* its record, and keeps concurrent projects
from sharing a mutable file. `<attempt-id>` is opaque, unique per attempt, and never reused — a
retried task has several attempt directories, not one overwritten pair of timestamps.

**Ownership.** The coordinator owns `dispatch.json`, `runtime.json` and everything under `receipts/`.
Exactly one assigned writer — a worker, or the capture helper acting for it (§10) — owns
`result.json`, `verification.json` and the two logs for its own attempt, and nothing else anywhere.

**Atomic visibility.** Every record is written to a temporary path in the same directory and made
visible by atomic replacement, so no reader ever observes a half-written record. This is the same
discipline `atomic_write_json` already applies to `project.json`.

### 5.2 What each record must carry

| Record | Essential contents |
|---|---|
| Dispatch | Project, task and attempt ids; coordinator generation; execution contract hash; input identities; allowed resources; verification definition **and its method** (§10.3); capacity and deadline terms |
| Result | Attempt id; contract hash; outcome (`done` / `failed` / `blocked`); artifact references with hashes; a concise handoff for the coordinator's context; references to verification receipts |
| Verification | The actual argument vector *or* the review method used; working directory; start and end times; exit status; output references; provenance class (§10.2) |
| Integration receipt | Attempt and result identity; accepted artifact and verification identities; the canonical revision that accepted them |
| Runtime bookkeeping | Active attempts; held reservations; dispatch pause state; host handles; reconciliation status |

Timestamps are timezone-aware RFC 3339, matching the rule canonical state already enforces on
authorization and receipt stamps. **Elapsed durations are measured on a monotonic clock and reported
in UTC**: a wall-clock difference across an NTP step is not a duration.

Dispatch, actual start, finish and integration are recorded **separately** wherever each is
observable. Collapsing them loses exactly the distinction the closing report needs (§13).

### 5.3 Disposable and durable, stated explicitly

The single most consequential classification in this document, because revision 1 got it wrong by
asserting that the store held nothing irreplaceable:

| Content | Class | Why |
|---|---|---|
| A mirror of task ids, levels, statuses | **Disposable** | Reconstructible from `project.json` at any revision |
| A dispatch record for an attempt that has not executed | **Disposable** | Re-preparable; §11 requires discarding rather than trusting it |
| **A result not yet integrated into canonical state** | **Durable** | Canonical state cannot reconstruct it. Losing it discards work that was actually performed |
| **A verification receipt not yet imported into evidence** | **Durable** | It attests an execution that happened once |
| An integration receipt | **Durable, immutable** | It is what a restart uses to recognise already-committed work (§11) |

**A store need not be authoritative for task status to hold irreplaceable evidence.** Revision 1's
invariant that "deleting the store loses at most in-flight claims" was false for exactly this reason,
and any later substrate (§17) inherits the obligation: retention and recovery rules, not just a
schema.

## 6. Attempts and the execution contract

### 6.1 The attempt lifecycle

```text
PREPARED
   │ canonical task committed RUNNING
   ▼
DISPATCHED → RUNNING → RESULT_READY → VERIFIED → INTEGRATED
                │
                └─ lost contact → UNCERTAIN → reconcile before any replacement
```

These are **execution-attempt** states. They never replace canonical task statuses, and
`project.json` remains the only answer to "what is this task's status" [C1]. A `failed` or `blocked`
result is a first-class result that reaches `RESULT_READY` and is integrated: an outcome must not
become unreachable merely because it cannot follow the successful path.

### 6.2 Identity, and what it is not

Every heartbeat and every submission carries the **attempt id and generation the coordinator
issued**. A message whose identity does not match the live attempt is rejected as stale.

Revision 1 claimed that SQL predicates over a caller-supplied `worker` name made cross-worker writes
impossible. They do not: `WHERE worker = ?` compares a string the caller chose. Parameterized SQL
prevents injection; it does not confer authority.

**[UNENFORCED]** Attempt identity rejects *stale protocol messages*. It is not filesystem isolation
and must never be described as such. A worker with shell access to the same user-owned files can
write outside its assignment, and — correcting revision 1 directly — can generally invoke the
launcher too, unless the host actually restricts its tools. Tokens and tool restrictions prevent
accidental misuse *within* the protocol. Isolation, where it is needed, comes from §8.

### 6.3 The execution contract hash

An attempt carries an immutable hash over the part of the task definition it executed against —
outputs, success criteria, verification and method, declared resources — plus the identities of its
inputs. The rules:

- The contract is **frozen for the life of the attempt**. A result is accepted only against the
  contract it ran under.
- **A sibling completing does not invalidate an attempt.** The project revision increments for
  legitimate unrelated reasons; treating a revision bump as invalidation would make concurrency
  self-defeating.
- If the executing task's own definition changes mid-flight, the attempt is **explicitly invalidated
  and its result quarantined for reconciliation** — not silently accepted, and not silently dropped.

## 7. Scheduling

### 7.1 Eligibility

Revision 1 defined eligibility as "every id in `depends_on` is `DONE`". That is one of seven
conditions. A task is **admissible** when all of these hold:

1. Its own status is `TODO` (or `BLOCKED` and explicitly being retried) — not `RUNNING`, `DONE` or
   `SKIPPED`.
2. Every id in `depends_on` is `DONE` in canonical state.
3. Its authorization requirements are satisfied. Effects of kind `destructive` or `external` stay
   with the coordinator, because authorization must be confirmed immediately before the action [C17]
   and confirmed explicit, current and exactly scoped at the moment the task starts [C18] — a
   subagent cannot hold that conversation with the user. **A `local_write` task that nevertheless
   records `authorization.required` is equally undelegable**, which revision 1's rule missed by
   keying only on effect kind.
4. No other attempt for it is active, `UNCERTAIN`, or awaiting reconciliation.
5. The project permits execution and dispatch is not paused (§12).
6. Its resource claims can be reserved (§7.2).
7. Host capacity is available (§15).

### 7.2 Admission on resources, not on outputs

Revision 1 required a task's declared outputs to be disjoint from every other claimable task's. That
is wrong in two directions at once.

**It is not sufficient.** Two tasks with different output files still conflict through: one reading a
file the other is changing; a shared `.coverage`, build, cache or temporary file; git's index,
checkout or branch state; a directory declaration that contains another task's child file; two
distinct rooted references resolving to the same physical location. The coordinator's own
verification commands and local actions participate in these conflicts too.

**It is not the right shape.** Excluding a task because *some* claimable task conflicts with it can
exclude both members of a pair. Usually one should run and the other should wait.

So admission works on **resource claims**, under a deterministic rule:

```text
Order ready tasks by priority (dependency depth, then id, for determinism).
Admit a task when its resource claims conflict with neither
  the reservations of active attempts
  nor the claims of tasks already admitted in this dispatch round.
Otherwise leave it ready; it will be considered next round.
```

Resource resolution rules, all of them necessary:

- **Resolve rooted paths before comparing.** `target`, `workspace`, `workspace_root` and `external`
  references must become absolute real paths first; two spellings of one location are one resource.
- **Directory ancestry is overlap.** A claim on a directory conflicts with a claim on anything
  beneath it.
- **Canonical files are protected explicitly**, never merely by convention: `project.json`,
  `spec.md`, `evidence.md`, `briefing.md`, `INDEX.md`, `MEMORY.md`.
- **Count reads when the input is mutable.** A reader of a file another attempt writes is a
  conflict, not a bystander.
- **One coarse exclusive resource for repository-wide operations** — a repo-wide test run, lint, or
  anything touching git index or branch state. Modelling git precisely is a research project; taking
  an exclusive resource is correct, cheap, and honest about what it costs.

### 7.3 Capacity

**Four concurrent attempts, as a default ceiling.** §2 shows four is within 0.04x of unlimited across
every measured project while mean maximum level width is 3.87, so a higher bound buys nothing this
sample can see, and costs a wider blast radius and more concurrent writers.

Four is **not a required worker count**, and the host may offer fewer: one observed host exposes four
total agent slots *including the coordinator*, leaving three for workers. The scheduler takes its
capacity from the host contract (§15) and treats four as an upper bound on it, never as a target.
Capacity one is the sequential path.

### 7.4 Reservations are held past submission

A reservation is taken at `PREPARED` and released only at `INTEGRATED`. Not at submission: if a
reservation dropped when the worker returned, the next admitted task could modify an artifact while
the coordinator was still verifying it, and the verification would describe a state that no longer
exists. This is also why a coordinator re-run (§10.4) must happen against stable inputs.

On lost contact the reservation is **held, not released** — see §11.

## 8. Isolation, as a progression rather than a promise

Revision 1 chose one filesystem mode (a shared working tree) and rejected git worktrees outright.
Both halves were too strong. Isolation is a property to be introduced progressively, and the mode is
part of an attempt's dispatch record:

| Mode | Suitable first use | Limits to state plainly |
|---|---|---|
| Read-only workers, separate result files | Surveys, comparative analysis, independent review | Nothing to isolate; the safest mode and the one to ship first |
| Shared target with explicit reservations | Cooperative workers producing bounded, disjoint files | Reservations are enforced by admission, not by the filesystem |
| Isolated staging directories | Plain-directory projects wanting safer retries and controlled promotion | Requires reconciling the skill's instruction to work in the actual target location, and a promotion step |
| Git worktrees | Repository tasks benefiting from independent working copies and integration checks | Separates working copies; **does not** prevent a worker from reading or writing other paths. Turns integration into a merge |

Worktrees stay available rather than rejected: the objection that several workspace projects target
plain directories argues for a *mode per project*, not against the mode.

**[UNENFORCED]** **Where two projects share a target directory, per-project scheduling state provides
no guarantee at all.** Either define target-level coordination or decline concurrent execution across projects that
share a target. This document declines, and names it as the gap it is.

## 9. The worker contract

A dispatch record gives a worker: its task id, name, success criteria and verification definition
verbatim from canonical state; its resource grant, as the complete list of what it may touch; its
attempt id and generation; its heartbeat obligation and deadline; and its filesystem mode.

A worker returns exactly one `result.json` (§5.2) at its assigned path, written atomically, plus the
verification receipt and logs for its own attempt. It returns a **concise handoff** rather than a
transcript: keeping worker output out of the coordinator's context is half the objective (§1).

The prohibitions:

- **Never write canonical state.** Not `project.json`, not `spec.md`, not `evidence.md`, not
  `INDEX.md`, not `MEMORY.md`.
- **Never write outside the resource grant.**

**[UNENFORCED]** Both are prompt text plus, at best, host tool restriction. Revision 1 claimed a
worker has "no tool that would let it" write canonical state; that is withdrawn (§6.2).

**[UNENFORCED]** A grant is also not verifiable as *complete*: an undeclared write is invisible to any
inference over declarations. A post-task comparison of touched paths against the grant is worth
building as **diagnostics** — and must not be presented as enforcement: with several concurrent
attempts it cannot reliably attribute a change to one of them, and it cannot see a write that was
later reverted.

## 10. Evidence integration

### 10.1 Capture, then import

Revision 1 offered a worker-written transcript that the coordinator scanned for the word "success",
and argued this was sound because a worker trusted to write outputs is trusted to write a transcript.
Those are different assurances. Permission to produce a deliverable does not establish that
verification executed; reading "success" in a file proves only that the file says so.

The replacement separates *capturing an execution* from *appending canonical evidence*:

1. A **capture helper** runs the actual command and records its argument vector, working directory,
   start and end times, exit status and output references.
2. It emits a structured **receipt** bound to the attempt and artifact identities.
3. The coordinator **imports** that receipt into `evidence.md` without re-running the command.
4. The evidence entry names the receipt's **provenance class**.

The point is not that this makes fabrication impossible. It is that no step asks a language model to
author its own process result: the exit status in a receipt came from a process, not from a sentence.

**[UNENFORCED]** A receipt written into a worker-writable location is not tamper-proof. This
document states that limit rather than implying otherwise; strengthening it is what §8's later modes
and a host's tool restrictions are for.

### 10.2 Provenance classes

Every evidence entry declares one, and the closing report shows it:

| Class | Meaning | Strength |
|---|---|---|
| **coordinator-executed** | The coordinator ran the command itself through `record-evidence` | Strongest. Its exit code and output tail come from a process it owned |
| **runner-captured** | The capture helper ran it and the coordinator imported the receipt | Strong. The execution is attested by a process record, in an environment the coordinator did not own |
| **worker-reported** | A worker asserted an outcome with no process record | Weakest. Admissible only where verification is genuinely non-executable (§10.3), and always labelled |

A report that does not distinguish these is a report that overstates its own evidence.

### 10.3 Verification is not always a command

The schema's `verification` field is defined as a "Command, inspection, or review that demonstrates
success" [C13]. Revision 1 treated every verification string as executable shell text, which is wrong
for a task whose check is a reading. The execution contract therefore carries a **method**:
`command` (an argument vector, capturable), `inspection`, or `review`. Only `command` can produce a
`coordinator-executed` or `runner-captured` receipt; the other two produce `worker-reported` evidence
and say so.

### 10.4 Re-running, and when it is valid

Coordinator re-execution stays available and remains the strongest class — it is the right choice for
a cheap independent check. One condition: it must run **against stable inputs**. A re-run performed
while sibling attempts are modifying the tree checks a different state from the one the worker
produced, which is why §7.4 holds reservations through integration.

### 10.5 Idempotence

Appending evidence must be idempotent on the accepted receipt identity. §11 depends on it: a restart
that finds evidence appended but no canonical commit has to retry integration without writing the
entry twice.

## 11. The recovery contract

The specification is not finished until every interruption has a defined outcome. Each row is an
obligation on the implementation and a test in §18.

| Interruption point | Required recovery |
|---|---|
| Attempt `PREPARED`, task not committed `RUNNING` | **No execution permitted.** Reconcile or discard the preparation; never treat a dispatch record as evidence of work |
| Task `RUNNING`, worker never launched | Reconcile the dispatch. Never invent a completed execution; re-dispatch under a new attempt id or return the task to `TODO` |
| Worker finished, coordinator has not read the result | The durable result remains **pending** and is read again. Reading never consumes |
| Evidence appended, canonical commit absent | Retry integration; the evidence append is idempotent (§10.5) so it is not duplicated |
| Canonical commit landed, acknowledgement absent | Recognise the exact integration receipt and acknowledge. **Do not re-run and do not re-record** |
| Canonical commit landed, index rebuild failed | The execution is committed. Repair the index; `project.json` remains authoritative [C11] |
| Worker contact lost | Attempt becomes `UNCERTAIN`; **reservations are held** until termination or isolation is established (§11.1) |
| The executing task's contract changed | Quarantine the result for reconciliation (§6.3) |

### 11.1 Lost contact is not a free retry

Revision 1's invariant made an expired lease immediately reclaimable. Expiry does not establish that
the previous worker stopped — and an agent can miss a deadline while legitimately blocked on a long
tool call. Rejecting the old worker's eventual submission protects the record and does nothing about
its filesystem writes; two writers in one tree is exactly the failure the design was meant to prevent.

The default is therefore:

1. Deadline passes → the attempt becomes `UNCERTAIN`.
2. Its reservations **remain held**.
3. The coordinator establishes that the old execution stopped, and inspects partial outputs.
4. A replacement attempt starts only after that reconciliation, under a new attempt id.

**[UNENFORCED]** A missed deadline is not evidence of a stopped worker, and a delivered heartbeat is
not evidence of progress: no deadline distinguishes useful work from a loop.

**Automatic retry without reconciliation is admissible in exactly one case:** the old attempt's
writes were confined to a disposable location whose outputs cannot be accepted after revocation —
which is what §8's staging mode buys.

### 11.2 One coordinator

A single-coordinator ownership guard, separate from `.project.lock`. The revision check prevents two
coordinators from *committing* conflicting state; it does nothing to stop them dispatching duplicate
physical work, which no later reconciliation can undo. Takeover reconciles every outstanding attempt
before dispatching anything. **The project lock is never held across agent execution or
verification.**

## 12. Failure semantics

On a result of `failed` or `blocked`:

1. The coordinator **persists a dispatch pause** in `runtime.json` before doing anything else, so an
   interruption cannot resume a run the failure should have stopped.
2. In-flight attempts are **not cancelled**; a half-finished task that has already written part of its
   outputs is worse than a finished one. Independent siblings may continue where continuing is
   appropriate.
3. Both of those run under a **bounded shutdown and reconciliation policy**. Revision 1 said "let
   every live lease finish", which can wait indefinitely. A bound with a defined action at expiry
   (§11.1) is required.
4. Completed sibling work is committed `DONE`. It happened, and discarding a true record to make a
   tidier failure story is the one thing this protocol may never do.

**The project stays `EXECUTING` while any task is `RUNNING`.** This is not a preference: validation
rejects a `BLOCKED`, `PLANNING`, `ALIGNING`, `REVIEW` or `DONE` project that has `RUNNING` tasks
[C12], so revision 1's "move to `BLOCKED` with a `block_reason`" would have been refused by the
commit it was specified to perform. `BLOCKED` is entered only once running work is reconciled and
nothing is `RUNNING`, and a `BLOCKED` project must contain at least one `BLOCKED` task [C12].

## 13. Timing and measurement

The task schema carries no start, no end, and no duration; a span is derived from the stamps
`record_evidence` wrote, and the code says so — it "measures verification, and only for tasks whose
verification was recorded at all" [C19].

**Timing lives first in receipts, not in the task schema.** Attempt records already carry dispatch,
start, finish and integration times (§5.2), which makes achieved parallelism measurable *without* a
schema change — so splitting `TASK_FIELDS` stops being the first implementation dependency, which is
what revision 1 made it. The cost, stated plainly: readers and reports must be taught to consult
these receipts. The existing task graph will not discover their timing on its own.

If per-task timing fields are added later:

- `TASK_FIELDS` is used as both the required set and the allowed set in the same two lines [C20]
  [C21], so a field cannot be optional until they are split:

  ```python
  TASK_FIELDS = {...}                                               # allowed
  TASK_REQUIRED_FIELDS = TASK_FIELDS - {"started_at", "ended_at"}   # required
  ```

- **Making a field optional in a new reader does not make new records readable by an old
  installation** that rejects unknown fields. Reader/writer compatibility must be stated explicitly,
  not assumed from the direction of the change.
- **Terminal tasks are never backfilled.** A retried task carries several attempt records; there is
  no single correct start time to write into it.

## 14. Authoring wider plans

The measurement in §2 says the executor is not the bottleneck, so this section is load-bearing — as a
**technique**, not as an objective (§1). `SKILL.md` already tells the planner to use a graph only
where independent work can run in parallel and to assign non-overlapping outputs [C7]. What it does
not say is how to find that independent work. Four patterns account for most avoidable narrowing:

1. **A false chain through a shared artifact.** Three tasks each appending a section to one document
   are ordered only because they name the same output path. Give each its own file and add an assembly
   task: three tasks at one level plus a join, instead of a chain of three.
2. **Verification folded into a successor.** A task whose verification is owned by a later task cannot
   be delegated, and cannot even reach `DONE` independently — the commit refuses a `RUNNING` dependent
   of a non-`DONE` dependency. Give every task a check it can run itself.
3. **A survey serialized by habit.** Reading five subjects to compare them is five independent tasks,
   and it is the shape that benefits most from context economy: five workers' worth of file contents
   never enter the coordinator's window.
4. **Setup tasks that are actually independent.** Fetching, branching, and scaffolding are often
   ordered by narrative rather than necessity.

And the counter-rule, because this guidance is easy to over-apply: **a plan that cannot be widened
should not be.** Splitting a document into artificial pieces raises average level width while adding
assembly and review work — the metric improves and the objective does not. The project that produced
this document is 5 tasks in 5 levels for good reasons (§2).

## 15. The host capability contract

A "host-capability gate" was promised in revision 1 and never specified. It is an operational
contract: before dispatching anything, the coordinator establishes each of these, and falls back to
capacity one when any is unavailable.

| Capability | What must be established |
|---|---|
| Spawning | The host can start a worker with a supplied prompt, and this skill's text asks for it |
| Bounded context | A worker's tool output does not enter the coordinator's window |
| Result delivery | The worker can write its assigned result path, and the coordinator can read it |
| Worker status | The coordinator can tell running from finished from gone |
| Interruption | Defined behaviour when the coordinator or a worker is interrupted |
| Capacity | The actual number of concurrent workers available, which may be below four (§7.3) |
| Tool restriction | Which restrictions, if any, the design's threat model may rely on — and none is assumed by default (§6.2) |

Cross-host parity is a repository requirement, not an aspiration [C14], and shipped scripts must run
stdlib-only on stock macOS Python 3.9.6 [C15] — which a file-based store satisfies with nothing but
`json`, `os` and `hashlib`. That is part of why it goes first.

**[UNENFORCED]** Nothing in this repository can inspect a host's agent configuration, so a
misconfigured worker fails at dispatch rather than at launch.

## 16. What this design does not enforce

Collected so that no reader has to infer it. Each appears marked **[UNENFORCED]** where it is stated.

| # | Rule | Why code cannot enforce it | Where |
|---|---|---|---|
| U1 | A worker writes only within its resource grant | Prompt text plus, at best, host tool restriction; the filesystem is shared in the modes that share it | §9 |
| U2 | A resource grant is the task's complete touch set | An undeclared write is invisible to an inference over declarations, and post-task comparison is diagnostics, not attribution | §9 |
| U3 | A capture receipt was not tampered with | The coordinator reads a file, in a location the worker may be able to write | §10.1 |
| U4 | Attempt identity isolates filesystem writes | It rejects stale messages only; a shell with file access bypasses the protocol | §6.2 |
| U5 | A heartbeat means progress | No deadline distinguishes useful work from a loop | §11.1 |
| U6 | A worker's agent type exposes what the protocol needs | Host agent configuration is outside a plugin's reach | §15 |
| U7 | Two projects sharing a target directory do not collide | Per-project scheduling state carries no cross-project guarantee | §8 |

**Why this section exists at all.** Revision 1 recorded a finding that `SKILL.md` documents a guard —
`init` and `research-validate` warning when the working directory contains this module but the running
tools come from elsewhere [C25] — that no code enforced, and named it "the precise failure mode a
design document is most likely to reproduce." The finding was false: `self_location_warnings` [C26] is
called from validation [C27], and it fired on this project's first `commit --dry-run`. The error was in
the search, not the repository — the grep covered the two CLI entry points and the tests but never the
module defining the function.

Revision 2 supplies the mirror image, and it is the more expensive one. §3.1's lock claim was a
statement that something *was* enforced, asserted from reading the same file, and it survived long
enough to select an architecture. **A claim that something is unenforced and a claim that it is
enforced are equally unverified until a citation is checked, and a citation that names the right
function is not the same as one that names the right line inside it.** Every claim in this document
cites §21, and the checker re-reads each cited range rather than trusting the table.

Two smaller findings, true and cheap to state: `SKILL.md` [C25] names `init` as a warning site where
this project observed none, and omits `commit`, which warned.

## 17. Rejected, deferred, and revisited

### 17.1 Deferred: SQLite, and a custom MCP server

Revision 1 specified scheduling state in a SQLite database at `<project-dir>/queue.db` in WAL mode,
reached by workers through a hand-written JSON-RPC-over-stdio MCP server. Both are deferred to Stage 7
(§18), admitted only against a need the benchmark (§19) actually demonstrates.

The reason is not that either is unworkable. It is that the justification was §3.1's void lock
measurement; that at four workers coordinator assignment needs no pull protocol at all (§4); that the
store's real requirement is recovery, which files with atomic replacement satisfy on the Python floor;
and that an MCP server would be the first such component in a repository that declares itself to three
hosts through three marketplace files carrying no MCP wiring, under a 100% coverage gate [C16].

If a later stage does adopt them, these are the conditions, recorded now so they are not rediscovered:

- **Attempt-based primary keys**, not task-based: a retried task has several attempts.
- **Explicit integration state** on every result row. Revision 1's schema had no field for whether a
  result had been read or integrated, which is what made its drain lossy.
- **Eligibility and reservation in one transaction**, or admission races.
- **Foreign keys enforced on every connection** — SQLite does not do this by default.
- **No transaction held across execution or verification.**
- **WAL-aware backup**: the WAL is persistent database state, so copying only the main database file
  can lose committed transactions.
- **A patched SQLite.** The measured runtime on the floor interpreter is stock `/usr/bin/python3`
  3.9.6 with SQLite **3.51.0**. The review reports a documented rare WAL concurrency corruption bug
  affecting that version, fixed in 3.51.3 (`https://www.sqlite.org/wal.html#walresetbug`). That
  reference could not be fetched in the session that wrote this revision, so it is recorded as the
  reviewer's finding with its source, and the obligation is to **probe the actual runtime** and choose
  a patched version, rollback journaling, or a connection architecture that avoids the affected access
  pattern. `PRAGMA journal_mode=wal` returning `wal` is not validation.
- **An explicit MCP protocol-version target.** Revision 1's `initialize` model corresponds to older
  versions; the published 2026-07-28 stdio specification uses per-request metadata and documents
  compatibility probing for legacy initialization. A hand-written adapter needs deliberate version
  coverage rather than one shape.
- **MCP adapts operations; it never owns scheduling or recovery semantics.**

The prior rejection of SQLite in this repository was narrower than it looks: it rejected FTS5 with the
compiled `sqlite-vec` extension [C22], neither of which any of this needs. That is a distinction, not
a reversal — and it is not an argument for adopting SQLite either.

### 17.2 Rejected

**A per-task `delegable` field the planner sets.** Rejected: it needs a default backfilled into every
existing project and adds a field a planner can get wrong. Admissibility is computed from facts
already required to be correct (§7.1).

**A queue canonical for task status, with `project.json` as a projection.** Rejected: it rewrites the
commit protocol rather than extending it, replaces optimistic revision checks with database
transactions, requires migrating every existing project, and demotes the auditable JSON record the
whole skill is built on.

**A workspace-root-wide store.** Rejected: one mutable file shared across projects means one
corruption affects all of them. It would not have solved the cross-project target collision either
(§8) — that is a resource problem, not a storage problem.

**A separate wave-synchronous scheduler.** Rejected in this revision: capacity one of the ready-queue
scheduler is sequential execution, and a second scheduler would be the path that runs on every
degraded host and is exercised least (§4).

**Consuming results on read.** Rejected: it loses completed work on a crash between read and commit,
with no API left that returns it (§11).

**Letting workers write canonical state or run `commit`.** Rejected, unchanged, on the ownership rule
[C5] [C6] and the revision-check protocol — not on any lock measurement.

### 17.3 Revisited from revision 1

**Git worktrees per worker.** No longer rejected. They are one of four filesystem modes (§8), chosen
per project, with their real limit stated: they separate working copies and do not confine a worker.

**Worker-run `record-evidence`.** Revision 1 rejected it on the void lock measurement. It stays
rejected on the sound reason: one writer on shared evidence. §10's capture helper gives a worker the
useful half — a real process record — without a second writer on `evidence.md`.

## 18. Implementation stages

Ordered so that each stage is testable before the next, and so that nothing behavioural ships until
everything under it is verified.

| Stage | Changes | Exit condition |
|---|---|---|
| 1. **Correct the specification** | This document: attempt states, recovery, resources, identity, provenance; every void-lock claim deleted | Every transition in §6.1 has a crash and retry outcome in §11 |
| 2. **Execution core** | `execution_lib.py`: admission, resource conflict resolution, contract hashing, attempt validation | Deterministic tests pass with no host and no subagent |
| 3. **Durable handoff** | `execution_store.py`: atomic records, immutable receipts, idempotent evidence import, repeatable delivery | Restart tests preserve both pending and committed work |
| 4. **Capture and isolation** | Capture helper reusing the existing process-capture path; provenance classes; the first two filesystem modes | A failed or stale verification cannot become accepted success |
| 5. **Host wiring** | Small per-host dispatch adapters plus the §15 capability checks | Each host demonstrates parallel operation *or* documented capacity-one fallback |
| 6. **Enable the skill** | The worker contract, the coordinator loop, report changes, aligned release versions across all three manifests and `uv.lock` | End-to-end scenarios pass on every affected host surface |
| 7. **Reassess infrastructure** | Benchmark context, duration, integration overhead and cost; only then consider SQLite or MCP against §17.1's conditions | A demonstrated need, or the stage closes with no change |

Constraints that bind every stage: new behaviour in focused modules rather than a substantially larger
`workspace_lib.py`, reached through the existing launcher conventions; stdlib-only on Python 3.9
[C15]; public functions typed while project state stays `dict[str, Any]`; malformed JSON producing
findings or `WorkspaceError` rather than a traceback; canonical commits keeping their revision check
and `project.json` commit point; post-commit index rebuilding still going through
`_rebuild_index_after_commit` [C11]; new tests under `tests/plugins/research/project/`; the 100%
coverage gate [C16]; and skill activation strictly after implementation and host validation.

**Stage 6 is the one that changes behaviour, and it is last for that reason.** Until it lands, an
agent may not spawn subagents unprompted, so the skill text asking for them is what makes this design
in-policy rather than an interesting document.

## 19. Verification and benchmark plan

### 19.1 The tests worth writing

Failures and interleavings, not happy paths:

- Simultaneous reservations cannot exceed capacity or acquire conflicting resources.
- An expired attempt cannot submit as a newer one, and cannot trigger an overlapping shared-tree
  retry.
- Replaying a result is harmless; a conflicting duplicate submission is rejected.
- **Every row of §11 resumes correctly** — this is the highest-value block in the suite.
- A sibling revision change leaves an attempt valid; a change to the executing task's contract does
  not.
- Directory, rooted-path-alias, canonical-file and verification conflicts are all detected.
- A worker failure pauses dispatch while successful siblings remain recordable, and the project stays
  `EXECUTING` until running work is reconciled [C12].
- Old project records and immutable terminal history are unchanged.
- Each host executes a small independent pair, a dependency join, and a failure-and-restart scenario.

Use an **injected clock** and controlled process synchronization rather than sleeps, and assert
observable invariants rather than mirroring helper implementations.

### 19.2 The benchmark, which ships

Two of this document's claims are currently unmeasured — that context economy is the larger benefit
(§1) and that lock contention is real (§3.1) — and one is reproducible only in principle (§2). So:

- The graph-shape script and its fixtures are committed with the figures.
- Coordinator context consumption is measured **separately** from total model usage.
- Worker startup, verification, integration, retries and end-to-end completion are recorded
  individually, so overhead is visible rather than absorbed.
- Capacity one is compared against larger capacities **under identical task contracts and
  verification requirements**. A comparison that lets the parallel run skip work is not a comparison.

## 20. Disposition of review 01

The full review is at `docs/parallel-execution-review.md`. It is recorded outside the skill's
`references/` deliberately: `references/` is shipped plugin content, and no test sweeps it —
`test_skill_docs.py` globs only `*/skills/*/SKILL.md` [C23] and the report-contract `SURFACES` tuple
names four specific files [C24] — so a review left there would ship to every installed host with
nothing checking it.

| # | Finding | Disposition |
|---|---|---|
| 1 | Store not reconstructible; drain consumes results | **Adopted.** §5.3 classifies contents; §11 makes delivery repeatable and acknowledgement post-commit |
| 2 | Lease expiry creates two writers | **Adopted.** §11.1: `UNCERTAIN`, reservations held, reconcile before replacement |
| 3 | Start protocol and stale-plan behaviour unspecified | **Adopted.** §7.1 widens eligibility to seven conditions, §6.3 adds the contract hash, §11 defines every gap |
| 4 | Disjoint outputs are not isolation | **Adopted.** §7.2 replaces output disjointness with resource admission; §8 makes isolation progressive; §9 demotes path comparison to diagnostics |
| 5 | SQL predicates do not establish identity | **Adopted.** §6.2 uses coordinator-issued attempt identity and states the honest threat model |
| 6 | Class B evidence weakens a deliberate guarantee | **Adopted.** §10 replaces it with capture receipts, three provenance classes, and non-executable verification methods |
| 7 | The lock argument does not justify the architecture | **Adopted.** §3.1 records the void measurement; the middle layer is rejustified on recovery; §17.1 defers SQLite and MCP |
| 8 | Transport and runtime gates need specifying | **Adopted.** §15 is an operational contract; §7.3 makes four a ceiling; §17.1 carries the MCP version target and the SQLite hazard, the latter as the reviewer's finding since it could not be re-verified here |
| 9 | Performance conclusions exceed the measurement | **Adopted on substance, wording answered.** §2 states what level width does not establish, §19.2 commits the benchmark, §1 demotes plan width to a technique, §12 and §17.2 drop the absolutism about commit batching. The finding says the design did not establish four workers "universally optimal"; the design claimed a measured 0.04x gap on this sample, not optimality. The substance stands; the strawman is not adopted |

## 21. Citations

Every claim above about current behaviour cites a row here. Each row names a file, a line range, and
text that must appear within that range; the checker for this document re-reads each range rather than
trusting the table.

| # | Source | Expected text within range |
|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py:1346-1349` | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py:2769-2776` | `Kahn's algorithm` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py:2791-2793` | `def build_task_graph` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py:2687-2688` | `def levels` |
| C5 | `plugins/research/skills/project/SKILL.md:181-185` | `Workers must not edit canonical state.` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md:11-14` | `sole writer of` |
| C7 | `plugins/research/skills/project/SKILL.md:384-385` | `Use a dependency graph only when independent work can run in parallel` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py:320-350` | `A cross-process lock based on atomic directory creation` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py:2542-2551` | `completed = subprocess.run(` |
| C10 | `plugins/research/skills/project/scripts/workspace_lib.py:2584-2596` | `must not see a half-written one` |
| C11 | `plugins/research/skills/project/scripts/workspace_lib.py:3226-3229` | `a failed index rebuild must not read as a failed commit` |
| C12 | `plugins/research/skills/project/scripts/workspace_lib.py:1362-1365` | `project cannot have RUNNING tasks` |
| C13 | `plugins/research/skills/project/references/workspace-schema.md:57` | `Command, inspection, or review` |
| C14 | `AGENTS.md:43-45` | `must work in Claude Code, Codex, and Kimi Code` |
| C15 | `AGENTS.md:73-76` | `stdlib-only` |
| C16 | `pyproject.toml:66` | `fail_under = 100` |
| C17 | `plugins/research/skills/project/SKILL.md:39-41` | `authorization immediately before` |
| C18 | `plugins/research/skills/project/SKILL.md:430-434` | `explicit, current, and scoped to the exact action` |
| C19 | `plugins/research/skills/project/scripts/workspace_lib.py:2612-2615` | `holds no start, no end, and no duration` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py:82-96` | `TASK_FIELDS = {` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py:1266-1267` | `_unexpected_fields(task, TASK_FIELDS` |
| C22 | `plugins/research/skills/project/references/memory-architecture.md:348-350` | `FTS5 is not guaranteed in a stock` |
| C23 | `tests/plugins/research/test_skill_docs.py:38-40` | `skills/*/SKILL.md` |
| C24 | `tests/plugins/research/project/test_report_contract.py:634-639` | `SURFACES = (` |
| C25 | `plugins/research/skills/project/SKILL.md:142-146` | `warn when the working directory contains` |
| C26 | `plugins/research/skills/project/scripts/workspace_lib.py:1667-1669` | `def self_location_warnings` |
| C27 | `plugins/research/skills/project/scripts/workspace_lib.py:1203-1204` | `self_location_warnings(working_directory)` |

One claim is deliberately **not** citable as a line range: that no claim resting on the void lock
measurement survives in this document. An absence has no line. Its checker assertion searches the
whole document for the retired phrases and fails if any returns.
