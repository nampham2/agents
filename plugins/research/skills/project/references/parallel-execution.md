# Parallel task execution

> **Status: design awaiting implementation.** Nothing in this repository implements this protocol.
> No script, schema field, MCP server, or `SKILL.md` step exists for it, and this document is not
> linked from `SKILL.md` precisely so that no coordinator is told to follow a protocol the tooling
> cannot execute. It is a specification for a successor project to build against. Until that project
> ships, `research:project` executes tasks sequentially and delegation is out of policy.
>
> **Revision 5, 2026-09-11.** The objective is **lower completion time and lower coordinator context
> consumption at preserved quality and controlled total cost**, for one `research:project` plan whose
> independent tasks are dispatched by one coordinator to subagents, published through a durable
> journal, and integrated into canonical state by that same coordinator, which remains the sole writer
> of it. Revision 4 promised more than it could establish. This revision **narrows the envelope**: it
> states five invariants and names, for each, the authority that holds it and the point at which it is
> synchronized (§1); it says exactly which task shapes and which hosts v1 supports (§3); and it
> **refuses everything else visibly, at admission** (§4), rather than running it under a rule that
> almost holds. Roughly half of revision 4's promises are demoted to §22.
>
> Two structural changes follow from that. Labels no longer gate anything: §6 defines *facts*, and the
> label is derived from them for display only, so recording a real asynchronous event never requires a
> transition the graph forbids. And every operation (§8) is a precondition over facts plus an ordered
> sequence of durable effects with a stated replay rule, so a crash has a prefix and the prefix has a
> completion.
>
> This document is the normative reference: current rules, schemas, operations and failure behaviour.
> It is self-contained, because `docs/` is not part of the installed plugin subtree — nothing an
> executing coordinator needs is outside this file. The history, the disposition of all four expert
> reviews, and the rejected alternatives are at
> [`docs/parallel-execution-decisions.md`](../../../../../docs/parallel-execution-decisions.md), a
> repository-only companion. Read that document to learn why a rule is what it is; read this one to
> implement it.
>
> Its structural claims are checked by `tests/plugins/research/test_parallel_execution_doc.py` and its
> protocol invariants by `tests/plugins/research/test_parallel_execution_model.py`, a design-stage
> stateful model with crash injection. Neither is an implementation, and neither establishes that the
> protocol is correct. §21 states exactly what each one settles, and what it cannot: in particular, a
> citation that resolves does not verify the claim standing next to it, and §21.1 gives the worked
> example of that failure inside this document.

## 1. The invariant ledger

Five invariants. Everything else in this document exists to hold one of them, and any rule that does
not trace to a row here is a convenience, not a safety property.

Each row names the **authority** — the single place a question is answered from — and the
**synchronization point** — the lock or atomic operation under which the answer is established. An
invariant with no synchronization point is a hope.

| Id | Invariant | Authority | Synchronization point | Refused rather than risked (§4) |
|---|---|---|---|---|
| I1 | At most one executable write authority exists for any conflicting resource | the workspace execution registry: one grant file per attempt (§10.4) | the registry lock, held across scan-and-insert, and across scan-and-remove | takeover of a dispatcher not proven dead (`R-LIVE-PREDECESSOR`) |
| I2 | No claim is released while any permitted executor can still write | the attempt's **execution scopes** (§6.1): the launch capability, the worker's process tree, and every check subprocess | one `sealed` record per scope, all present, before the `release` operation's precondition holds | detached or background children (`R-DETACHED-CHILD`) |
| I3 | An acceptance binds the current task definition to the bytes that were checked | `project.json` at the accepting commit, plus each check's after-snapshot (§11.3) | the accepting commit under `.project.lock`, carrying `expected_revision`, `expected_run`, `expected_generation` and a re-derived `definition_hash` | inputs or per-check writes that cannot be enumerated (`R-UNENUMERABLE`) |
| I4 | Only a canonical receipt proves a commit landed | the `evidence` array of the task in `project.json` | `commit-observed` is written only after re-reading `project.json` at the committed revision and finding the receipt tuple | nothing; the rule costs one read |
| I5 | A malformed, stale-generation or unreadable record is never read as absence | record validation, before any projection (§6.3) | validation runs inside the projection, per record, and yields `INDETERMINATE` for the attempt | a record the current generation cannot explain (`R-INDETERMINATE`) |

Two consequences are worth stating in the ledger itself, because revision 4 got both wrong.

- **I1 is about capability, not observation.** "No worker is running now" and "no worker can be
  started later" are different propositions. A reservation protects a resource only while it is held,
  so releasing it on the strength of a `never_started` answer is unsound whenever a paused caller
  still holds an unconsumed launch capability. §12.2 therefore requires the capability to be consumed
  or the predecessor to be proven dead; absence of a process is not proof of either.
- **I2 is about sealing, not exiting.** Publishing a result does not stop a worker, and neither does
  a dead recorded process stop its descendants. The barrier is an explicit `sealed` record per scope,
  and the coordinator's own check subprocesses are scopes like any other — they are not invisible
  work performed under someone else's grant.

## 2. Foundations that already exist

This is not a thin protocol. It adds a durable execution journal, a workspace admission registry, a
capture and acceptance pipeline, a recovery contract, a schema activation gate, and at least one host
adapter, and §20 sequences that work honestly. What it does *not* have to build is the following,
which is shipped and tested today.

- **Concurrent `RUNNING` tasks are already legal.** `current_tasks` is validated against the exact
  set of `RUNNING` task ids, not against a maximum of one [C1]. Two running tasks need no schema
  change.
- **The scheduler's input is already computed.** `_dependency_levels` assigns each task a level by
  Kahn's algorithm — 0 with no prerequisite, else one past the deepest [C2] — and `build_task_graph`
  orders tasks by `(level, id)` [C3] while `TaskGraph.levels` counts distinct levels [C4]. Levels are
  useful for *presentation*; §10 schedules on satisfied dependencies, not on level barriers.
- **The ownership model is already normative.** One coordinator owns writes to `project.json`, shared
  records and `INDEX.md`; workers may write only assigned non-overlapping output paths and must not
  edit canonical state [C5], stated identically in the schema reference [C6]. The planner is already
  told to use a dependency graph only where independent work can run in parallel, to assign
  non-overlapping outputs, and to keep one canonical writer [C7].
- **Cross-process exclusion already exists.** A `mkdir`-based `DirectoryLock` [C8] plus
  `commit --expected-revision` gives optimistic concurrency: two writers means one loses, reloads and
  reconciles. Its default timeout is 5.0 seconds [C24], and `commit_candidate` inherits that default
  [C25] — §15 states those numbers rather than inventing better-sounding ones.
- **Single-file atomic replacement already exists and is correct.** `atomic_write_text` writes a
  sibling temporary file, flushes it, `fsync`s the descriptor, `os.replace`s it into position and then
  `fsync`s the containing directory [C9]; `atomic_write_json` is that function plus `json.dumps`. A
  reader never observes a half-written file. What it does not provide is **ordering across files** or
  **refusal to overwrite** — `os.replace` clobbers by definition — which is why §7.2 exists.
- **Canonical state already carries the enums this protocol keys on.** Task statuses [C22] and
  transitions [C20] are fixed maps; effect kinds are `none`, `local_write`, `destructive`, `external`
  [C10]; authorization statuses are `not_required`, `pending`, `explicit`, `denied`, `deferred` [C17],
  with `required` and `status` cross-checked [C18], an explicit-authorization rule that fires only for
  `RUNNING` and `DONE` tasks [C19], and a rule that `source` and `authorized_at` are null unless the
  status is `explicit` [C23]. Every canonical mutation in §8 is written against those values, and
  §8.1 gives each one a complete candidate rather than a status arrow.

So five things are missing, and they are what this document supplies: a **scheduler** (§10), a
**worker contract** (§16), a **publication and acceptance protocol** (§7, §11), a **recovery
contract** (§13), and a **resource model** (§9), each restricted to the envelope of §3.

## 3. The safety envelope

### 3.1 What v1 supports

The envelope is the conjunction of every row below. A plan, host or workspace that falls outside any
row is refused by the matching code in §4 — not degraded, not run under a weaker rule.

| Dimension | v1 supports | Held by |
|---|---|---|
| Filesystem | one shared POSIX filesystem; the execution store, its temporary files and every declared output on **one** volume; `link(2)` available and `O_EXCL`-equivalent | §7.2, `R-NO-ATOMIC-LINK`, `R-CROSS-VOLUME` |
| Failure model | process crash, kill, and host restart. Power loss and OS crash are covered only where §7.5 says a barrier is issued | §7.5 |
| Workspace | one workspace root, one project per coordinator run; **every** installation that mutates this workspace or the target working directory understands schema v4 and participates in the registry | §7.6, `R-LEGACY-WRITER` |
| Processes | no detached, background or resumable execution: an attempt's every scope terminates before it is sealed | §6.1, §16, `R-DETACHED-CHILD` |
| Task shape | `effect.kind ∈ {none, local_write}`; inputs, outputs and each check's read and write sets exhaustively enumerable as local file paths; every output path a regular file | §9, `R-UNENUMERABLE`, `R-DIRECTORY-SUBJECT`, `R-EFFECT-NOT-CONFINED` |
| References | `target`, `workspace` and `workspace_root` outputs only. `external` references are URL-like, not paths, and no rule here maps them onto claims | §9.2, `R-EXTERNAL-REFERENCE` |
| Ownership | a coordinator takes over only from a predecessor proven dead; otherwise the predecessor's grants stay held and the project stays stopped until an operator resolves it | §12.2, `R-LIVE-PREDECESSOR` |
| Capacity | at most four concurrent attempts, counting reserved and ambiguous launches, and counting the coordinator's own inline work | §10.3 |
| Hosts | one adapter, conforming to §17's matrix, with a tested `start`/`observe`/`seal`/`terminate` contract. Every other host runs inline at capacity one | §17, `R-NO-ADAPTER` |

Inline execution at capacity one is inside the envelope and is the fallback for every refusal that
does not itself make the store unsafe. It is the same scheduler at capacity one (§10.2), not a second
code path, and it is not exempt from the registry: an inline executor takes a grant like any other, so
a legacy writer in the same workspace is a conflict the registry can see.

### 3.2 The measurement that sets the ceiling

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
Loaded honestly, some of these projects would run *slower* in parallel. Dependency-level width is a
*structural* property of a plan; achieved scheduling depends on task durations, resource conflicts,
verification and integration cost, and host capacity, none of which appears above. The script and
graph fixtures behind the table are committed by Stage 2 (§21.3); until then the figures are
reproducible in principle and **not yet reproducible in this repository**.

Three consequences shape everything below.

- **Wall-clock speedup is not the justification.** A design sold on 1.45x, measured optimistically
  against overheads it does not count, would not survive contact with a real project. Context economy
  is the plausible larger win on a host with bounded worker context (§17), and it is a hypothesis with
  a benchmark attached (§21.3), not an established benefit.
- **Four is a cheap ceiling.** It is within 0.04x of unlimited across all 31 projects, and mean
  maximum level width is 3.87, so a higher bound buys nothing this sample can see. §10.3 states it as
  a ceiling with that reason, not as a required worker count and not as an optimum.
- **The structural limit is in the plans, not in any executor.** Executor overhead was never measured,
  so no comparison between the two is available. §18 exists for that reason.

The project that produced this document has 5 tasks in 5 levels — average width exactly 1.0. Fetch a
branch, write a document, check it changed nothing, commit it, publish it. **Not every plan can be
widened**, and a design that assumes otherwise mistakes a property of the work for a defect in the
planner.

## 4. Refusals

A refusal is a **first-class, recorded outcome**, not an error path. The coordinator names the code,
names the subject that triggered it, and says what it did instead. Refusals are cheap to add and
cheap to remove later; an ambiguous promise is neither.

Each code below is detected at exactly one point, so no case is checked twice with two answers.
`activation` is `enable-execution` (§7.6), `admission` is the ready-loop gate (§10.1), `preparation`
is the `prepare` operation (§8.1), and `closure` is `research-validate --close`.

| Code | Condition | Detected at | Effect |
|---|---|---|---|
| `R-NO-ATOMIC-LINK` | the store's filesystem cannot link a file without following or clobbering an existing name | activation | the whole protocol is unavailable, parallel and inline alike; the project stays on today's sequential path |
| `R-CROSS-VOLUME` | a declared output, or the store's temporary directory, is on a different volume from the store | activation, and again at preparation for each output | activation refuses; preparation refuses that task |
| `R-LEGACY-WRITER` | any installation that may mutate this workspace or working directory does not understand schema v4 | activation | activation refuses, naming the quiescing prerequisite (§7.6) |
| `R-NO-ADAPTER` | the host has no adapter conforming to §17 | activation | capacity one, inline only; parallel dispatch unavailable |
| `R-EFFECT-NOT-CONFINED` | `effect.kind ∉ {none, local_write}` | admission | the task runs inline on the coordinator, which is where a `destructive` or `external` effect and its authorization belong |
| `R-UNENUMERABLE` | the execution plan (§9.1) cannot list the task's reads, its writes, or a check's writes | admission | the task runs inline, holding a coarse claim on the narrowest enclosing declared root |
| `R-DIRECTORY-SUBJECT` | a check subject or an output is a directory | admission | the task runs inline; §9.2 has no digest for a directory and §22 defers one |
| `R-EXTERNAL-REFERENCE` | an output's root is `external` | admission | the task runs inline; the effect receipt stays with the coordinator |
| `R-CHECK-UNCOVERED` | a `required: true` output has no check whose subject set contains it | admission | the task is not admitted at all, in parallel or inline: the plan cannot show the output was produced |
| `R-DETACHED-CHILD` | an adapter cannot promise that the attempt's process tree has exited when it reports completion | activation, per operation in §17's matrix | that adapter is not usable for asynchronous dispatch |
| `R-LIVE-PREDECESSOR` | ownership is stale but the predecessor is not proven dead (§12.2) | activation | no takeover; existing grants stay held; the coordinator reports the operator action needed |
| `R-INDETERMINATE` | any record of an active attempt fails validation, or carries a generation the current run cannot explain | admission, and inside every projection | the attempt is `INDETERMINATE`; dispatch is blocked and no claim is released until an operator resolves it |
| `R-CAPACITY` | granting would exceed four concurrent attempts including reservations | admission | the task waits; this is the one refusal that resolves itself |

Two refusals deserve their reason stated, because they cost real capability.

- `R-UNENUMERABLE` is the load-bearing narrowing of this revision. Revision 4 derived read claims from
  dependency outputs and check subjects only, which cannot express an analysis task reading a source
  file that no dependency produced, a check that writes a cache, or a repository operation that
  rewrites a working tree. Rather than compare claims that are known to be incomplete and call the
  comparison prevention, v1 admits to parallel execution only the task shapes whose reads and writes
  the plan can list, and sends everything else through the coordinator. §9.1 says what a plan must
  list; §19 records that undeclared writes remain outside enforcement.
- `R-CHECK-UNCOVERED` refuses admission entirely rather than falling back to inline, because the
  defect is in the plan, not in the executor. A required output nothing checks is a task whose success
  cannot be established by any executor.

## 5. Architecture

```
                 ┌──────────────────────────────────────────────────┐
   canonical ────│  project.json   (revision-checked commit)         │  coordinator writes only
                 │  execution: { protocol_version, coordinator_run,  │  the fence for §12
                 │               ownership_generation, attempts }    │
                 └──────────────────────────────────────────────────┘
                                    ▲ integrates accepted results (§11)
                                    │
                 ┌──────────────────────────────────────────────────┐
   durable   ────│  <project-dir>/execution/   (§7.1)                │  append-only,
                 │  plans/ · attempts/<id>/<record>.json             │  publish-if-absent (§7.2)
                 └──────────────────────────────────────────────────┘
                                    ▲ grants
                 ┌──────────────────────────────────────────────────┐
   workspace ────│  <workspace-root>/.execution-registry/            │  every executor,
                 │  registry.lock/ · grants/<attempt-id>.json (§10.4)│  inline included
                 └──────────────────────────────────────────────────┘
                     ▲ start (down)         ▲ result (up, published per §8.1)
                     │                      │
              ┌──────┴─────┐  ┌────────────┐  ┌────────────┐
              │  worker 1  │  │  worker 2  │  │  worker N  │   N ≤ 4 (§10.3)
              └────────────┘  └────────────┘  └────────────┘
                 one shared filesystem, one volume for the store (§3.1)
```

Four properties define it.

1. **The coordinator assigns; workers do not claim.** At four workers the coordinator already owns
   every decision before execution (admission, authorization, resources, capacity) and after it
   (verification, evidence, commit). A worker discovering its own next task adds a distributed protocol
   to a problem that has none.
2. **Facts are recorded; labels are derived.** Every decision is a precondition over the facts of §6.1,
   computed from *validated* records and canonical state. The label of §6.2 is a projection for humans.
   Nothing reads a label to decide anything, so an event that the display cannot summarize is still an
   event the journal can record.
3. **The middle layer is durable, not derived.** Its justification is recovery: a coordinator that dies
   holding an uncommitted result must not lose completed work. That means classified durability (§7.5),
   a stated retention policy (§7.7), and a no-clobber publication primitive (§7.2).
4. **There is one scheduler.** Sequential execution is this scheduler at capacity one — not a second
   mechanism, and not an exemption from the registry (§10.4). A separate wave-synchronous path would be
   the code that runs on every degraded host and is therefore exercised least, which is how a fallback
   rots.

## 6. Facts, and the label derived from them

### 6.1 The fact set

`facts(attempt)` is computed from three authorities: `project.json`, the attempt's **validated**
journal records (§6.3), and the registry. It is a record of independent fields, not a state. Any
number of them may be true at once, including combinations no label distinguishes.

| Group | Fact | Type | Read from |
|---|---|---|---|
| canonical | `task_status` | one of `TASK_STATUSES` | `project.json` |
| canonical | `bound_attempt` | attempt id or null | `execution.attempts[task_id]` |
| canonical | `revision`, `run`, `generation` | integer, string, integer | `revision`, `execution.coordinator_run`, `execution.ownership_generation` |
| canonical | `authorization_in_force` | bool: `required` is false, or `status` is `explicit` | the task's `authorization` [C18] [C19] |
| canonical | `receipt_present` | bool: the receipt tuple of §11.3 is in the task's `evidence` | `project.json` |
| plan | `plan_hash`, `definition_hash` | digests | `plans/<task-id>.<plan-hash>.json`, §9.1 |
| journal | `reserved`, `prepared`, `launched` | bool | the matching record, valid |
| journal | `result` | null, or `success` / `failure` / `refused` | `result.json`'s `outcome` |
| journal | `declared_scopes` | set of scope ids | `prepared.json` plus every check dispatched since |
| journal | `sealed_scopes` | set of scope ids | `sealed/` |
| journal | `open_scopes` | `declared_scopes - sealed_scopes` | derived |
| journal | `open_causes` | set of cause ids with a `hold` and no matching `resolution` | `holds/`, `resolutions/` |
| journal | `stop_evidence` | null, or one of §13's kinds | `stop-evidence.json` |
| journal | `classified` | null, or a class from §14 | `classification.json` |
| journal | `accepted`, `commit_observed`, `released` | bool | the matching record, valid |
| journal | `uncertain_start` | bool: the adapter answered `ambiguous`, or the caller died inside `start` | `launch.json`'s `start_outcome`, or its absence beside a consumed capability |
| registry | `grant_present` | bool | `grants/<attempt-id>.json` |
| registry | `launch_capability` | `none`, `issued`, `consumed`, or `revoked` | the grant's `capability` field, §12.2 |
| validity | `indeterminate` | bool, with the offending record named | §6.3 |

**Execution scopes** are the unit I2 is stated over. An attempt declares one scope per thing that may
write on its behalf: `worker` (the dispatched process tree, or the coordinator's own inline execution),
and `check:<check-id>` for every check the coordinator runs itself (§11.5). A scope is **sealed** when
a `sealed` record exists for it, which the adapter may write only when the scope's whole process tree
has exited and the host cannot resume it. Adding a check adds a scope; that is why "the coordinator's
verification is not covered by the attempt's grant" is not expressible here.

### 6.2 The derived label

**Thirteen rows, thirteen labels, one row each.** `RESERVED`, `PREPARED`, `DISPATCHING`, `RUNNING`,
`PUBLISHED`, `UNCERTAIN`, `CLASSIFIED`, `HELD`, `QUARANTINED`, `ACCEPTED`, `COMMITTED`, `RELEASED`,
`INDETERMINATE`.

`label(facts)` is a total function: the first matching row wins, and the last row matches everything
that reaches it. It exists for the console summary, the closing report, and this document's prose.

| Row | Label | Matches when | Not usable for |
|---|---|---|---|
| 1 | `INDETERMINATE` | `indeterminate` | anything: it is the absence of an answer, and §4's `R-INDETERMINATE` is the only response |
| 2 | `RELEASED` | `released` | proving the executor stopped — `sealed_scopes` proves that, and O11 checks it before writing `release` |
| 3 | `COMMITTED` | `commit_observed` | proving the canonical commit landed — `receipt_present` does that (I4) |
| 4 | `ACCEPTED` | `accepted` | proving the task is `DONE`; acceptance is an intent, the commit is the fact |
| 5 | `QUARANTINED` | `open_causes ≠ ∅` and `open_scopes ≠ ∅` | deciding a release is safe; a disposition changes this label and seals nothing |
| 6 | `HELD` | `open_causes ≠ ∅` | implying every cause is resolved by one disposition; causes resolve individually (O8) |
| 7 | `CLASSIFIED` | `classified` | clearing the dispatch gate; §14 clears it on resolution, not classification |
| 8 | `PUBLISHED` | `result` is not null | implying the worker stopped; publication and sealing are independent facts |
| 9 | `UNCERTAIN` | `uncertain_start`, or the deadline passed with no `result` | implying a late `result` may not be recorded; O5 accepts one at any time |
| 10 | `RUNNING` | `launched` | implying canonical `task_status` is `RUNNING`; a prelaunch disposal leaves `TODO` |
| 11 | `DISPATCHING` | `launch_capability` is `issued` | implying no worker exists; the capability may already have been used |
| 12 | `PREPARED` | `prepared` | implying no claims are held; the grant precedes `prepared` (O2) |
| 13 | `RESERVED` | anything else, including a grant with no `prepared` | implying nothing was started |

The right-hand column is the point of the table. Revision 4 used labels as permissions, and every
event the graph could not express — a worker publishing before its launch record landed, a late result
after a timeout, a human review arriving after a hold, a prelaunch cancellation of a `TODO` task,
superseding an attempt that had to be released first — became either an unrecordable fact or an
unguarded transition. Facts have none of those problems: a late result is `result` becoming non-null,
whatever the display says.

### 6.3 Validity, and why absence is not a fact

Every projection **validates each record before reading it**. Validation is: the file parses as a JSON
object; `record_kind` is known; `schema_version`'s major equals 1; every required field of §7.4 is
present with the right JSON type; `attempt_id`, `project_id` and `task_id` match the attempt being
projected; `body_digest` recomputes; and `ownership_generation` is one this run can explain — either
the current generation, or an earlier one whose takeover is itself recorded (§12.2).

A record that fails any clause sets `indeterminate`, names the file, and stops the projection. It is
never treated as missing, and a "terminal" appearance never suppresses it: `release` and
`commit-observed` are validated exactly like everything else, which is what makes I5 a property of the
reader rather than a hope about writers.

No fact is derived from file presence alone. `heartbeat` is the sole exception to durability (§7.5) and
is never an input to anything but §13's staleness bound, which has an independent fallback in
`launch.written_at`.

## 7. The execution store

### 7.1 Layout

```
<project-dir>/execution/
├── config.json                              # settings and volume probes, §7.6, immutable per generation
├── plans/<task-id>.<plan-hash>.json         # the resolved execution plan, §9.1, immutable
├── owners/<coordinator-run>.json            # who owns the project, and what would prove it dead, §12.1
├── attempts/<attempt-id>/
│   ├── reservation.json                     # identity, written before the grant exists
│   ├── prepared.json                        # the contract, §9.1
│   ├── launch.json                          # start outcome and handle
│   ├── result.json                           # the worker's manifest
│   ├── sealed/<scope-id>.json               # one per execution scope, §6.1
│   ├── captures/<check-id>/<NNNN>-<capture-id>.json
│   ├── logs/<check-id>/<NNNN>.out            # truncated to config.log_cap bytes
│   ├── logs/<check-id>/<NNNN>.err
│   ├── classification.json
│   ├── holds/<cause-id>.json
│   ├── resolutions/<cause-id>.json
│   ├── stop-evidence.json
│   ├── acceptance.json
│   ├── commit-observed.json
│   └── release.json
└── runtime/
    ├── heartbeat/<attempt-id>.json          # advisory, overwritten in place, never journaled
    └── projection.json                      # a human-readable mirror; no decision reads it
```

`runtime/` is not an authority and is not a cache with a validity story: after any restart or ownership
change it is deleted and rebuilt from the journal. Volume and link probes, which *are* consulted during
derivation, live in `config.json`, established once at activation under the project lock, because a
probe result that decides whether a rename is atomic must not be re-answered by whatever file happens
to be lying around.

### 7.2 Publish-if-absent

Every D1 record (§7.5) is published with one primitive.

```text
publish_if_absent(final: Path, body: bytes) -> "created" | "identical" | "conflict"
  1. refuse unless final's parent and the store's temporary directory are on the same volume
     (config.same_volume, established at activation; otherwise R-CROSS-VOLUME)
  2. create every missing ancestor directory of final, and fsync each newly created ancestor's parent
  3. write body to tmp/<random>.json: single write loop until every byte is written, fsync the fd, close
  4. link(tmp, final)
     EEXIST -> read final; equal bytes -> "identical"; different -> "conflict"
  5. fsync the directory containing final
  6. unlink tmp, then fsync its directory
  7. return "created"
```

Four properties, each of which revision 4 stated incompletely.

- **`link` is the no-clobber operation.** `os.replace` clobbers by definition [C9], so the primitive
  that makes a record immutable cannot be built from it.
- **`identical` still owes the barrier.** A predecessor may have linked `final` and died before step 5.
  An identical-byte retry therefore performs steps 5–7 before returning `identical`; only then is the
  record durable. Returning early is the bug this bullet exists to forbid.
- **Every newly created directory entry needs its own barrier.** Syncing the innermost directory says
  nothing about the ancestor entry that names it, so step 2 syncs each one it creates.
- **`conflict` is an observed event, not a property of the final record.** The winning bytes are
  intact, and the losing bytes are *not in the store*. The observer therefore raises a
  `conflicting_publication` cause (§8.1, O7) carrying the rejected body's digest and length, and — when
  the observer is a worker, which may not write a coordinator-owned hold — writes
  `attempts/<id>/captures/_conflict/<NNNN>-<capture-id>.json`, a record the worker *is* permitted to
  write, which the classifier reads. A conflict that is never reported is the failure mode this rule
  removes.

I/O errors are normalized: `ENOSPC`, `EIO`, `EROFS` and a short write that cannot be completed all
raise a store error, which leaves the attempt `INDETERMINATE` rather than reporting absence. A
temporary file older than `config.tmp_reap` seconds with no matching live attempt is removed at
activation and at takeover, never during normal operation.

What publish-if-absent does *not* do is establish authorship. It refuses to clobber; it cannot tell a
coordinator's record from a hand-written file with the same name and a recomputable `body_digest`, so a
store edited by hand reads as history [UNENFORCED U5].

### 7.3 Canonical serialization and identifiers

One encoding, so a digest computed anywhere matches a digest computed anywhere else.

- UTF-8, no byte-order mark. Any input containing a lone surrogate or a codepoint that cannot round
  trip through UTF-8 is rejected at validation; the encoder never substitutes replacement characters.
- JSON object keys sorted by Unicode code point; separators exactly `","` and `":"`; no other
  whitespace. Arrays keep the order §7.4 states for that field, and every identity-bearing array is
  explicitly ordered there — never "some order".
- Integers only, written without exponent or fraction, for every field that enters a digest. No float
  ever does.
- **Exactly one trailing newline** (`\n`) after the closing brace. Not zero, not two, not "if any".
- `body_digest` is `sha256` over the canonical serialization of the record with the `body_digest` key
  itself removed, hex-encoded lowercase. Unknown fields introduced by a higher *minor* version are
  preserved and **included** in the digest, so a v1.0 reader still recomputes what a v1.1 writer wrote;
  they are excluded from every decision.
- `attempt_id` is `att-` plus 32 lowercase hex characters from the platform CSPRNG. It carries no
  meaning; task, generation and counter are fields.
- `capture_id` is `cap-` plus the first 32 characters of the capture's `body_digest`, making captures
  content-addressed with no self-reference. It appears in the filename as well as inside the record, so
  a receipt naming `(check_id, sequence, capture_id)` resolves to a path by pattern, without an index.
- `sequence` is a JSON integer from 1; the filename encodes it zero-padded to four digits. §11.4 caps
  executions of one check within one attempt at 16, so the encoding cannot overflow. Heartbeat records
  have no sequence at all — they are overwritten, not appended (§15).

### 7.4 Record schemas

Every record carries the same envelope, and validation (§6.3) requires all of it.

| Field | Type | Notes |
|---|---|---|
| `record_kind` | string | one of the kinds below; an unknown kind is a validation failure |
| `schema_version` | string | `"1.0"`; major 1 required, minor forward-compatible per §7.3 |
| `project_id`, `task_id`, `attempt_id` | string | must match the attempt being projected |
| `coordinator_run`, `ownership_generation` | string, integer | the writer's run and generation (§12.4) |
| `writer` | string | `coordinator`, `worker` or `adapter` |
| `written_at` | string | RFC 3339 UTC with a `Z` suffix and second precision |
| `body_digest` | string | §7.3 |

Kind-specific fields, all required unless marked optional:

| Kind | Fields |
|---|---|
| `reservation` | `plan_hash`, `claims` (ordered list of §9.2 claim strings), `counter` (integer, attempts of this task so far) |
| `prepared` | `definition_hash`, `plan_hash`, `contract` (§9.1), `baseline` (map: claim string → digest or `absent`), `declared_scopes` (ordered list), `deadline_at` (RFC 3339 UTC), `deadline_budget_s` (integer > 0), `heartbeat_interval_s` (integer > 0), `log_cap_bytes` (integer > 0) |
| `launch` | `start_outcome` (`started`, `no_start`, `ambiguous`), `handle` (optional string, required when `started`), `capability_id`, `started_at` (RFC 3339 UTC), `adapter` (name and version) |
| `result` | `outcome` (`success`, `failure`, `refused`), `produced` (map: output claim → digest or `absent`), `captures` (ordered list of `(check_id, sequence, capture_id, body_digest)`), `summary` (string ≤ `config.summary_cap`), `refusal_code` (optional, required when `refused`) |
| `sealed` | `scope_id`, `exit` (§11.2 exit status variant), `tree_exited` (bool, must be true), `resumable` (bool, must be false) |
| `capture` | §11.2 |
| `classification` | `class` (§14), `evidence` (ordered list of capture references), `conflict_digests` (optional) |
| `hold` | `cause_id`, `cause_class` (one of §14.1's fourteen), `detail` (string), `raised_by` (`coordinator` or `operator`) |
| `resolution` | `cause_id`, `resolution` (`rerun`, `accept`, `retry`, `block`, `skip`, `withdraw`, `stop`), `decided_by`, `rationale`, `evidence` (optional ordered list) |
| `stop-evidence` | `kind` (§13), `observed_at`, `detail`, `scopes` (ordered list of scope ids this evidence covers) |
| `acceptance` | `intent` (`DONE`, `BLOCKED`, `SKIPPED` or `TODO`; it is the durable intent of *any* canonical mutation, §8.1), `receipt_id`, `definition_hash`, `accepted_snapshot` (map: output claim → digest or `absent`), `qualifying_captures` (ordered list of capture references), `expected_revision`, `expected_run`, `expected_generation` |
| `commit-observed` | `receipt_id`, `committed_revision` (integer), `evidence_index` (integer, the position in the task's `evidence` array), `committed_status` (the task status read back) |
| `release` | `receipt` (`receipt_id` or null when no acceptance was committed), `sealed_scopes` (ordered list, must equal `declared_scopes`), `grant_removed` (bool), `reason` |
| `owner` | `host_id`, `boot_id`, `pid`, `process_start`, `started_at`, `generation` (§12.1) |
| `heartbeat` | `phase` (free string), `observed_at`; D2 only, never journaled |

`receipt_id` is `rcp-` plus 32 hex characters of the `sha256` of the canonical serialization of
`{attempt_id, definition_hash, intent, accepted_snapshot, qualifying_captures}` — the acceptance's own
`receipt_id` field is excluded from its own hash, and no other field name is used for it anywhere in
this document.

### 7.5 Durability classes and the failure model

| Class | Records | Barrier | Survives |
|---|---|---|---|
| D1 | every kind in §7.4 except `heartbeat`, plus `plans/`, `config.json`, captures and logs | §7.2 in full, including the barrier on the `identical` path | process crash, kill, host restart, and OS crash or power loss for any record whose step 5 returned |
| D2 | `heartbeat`, `runtime/projection.json` | none; overwritten in place with `atomic_write_json` [C9] | nothing that matters; both are rebuilt or discarded |
| worker outputs | the task's declared output paths | the worker fsyncs every output file and its parent directory **before** writing `result.json` | the same as D1; without this rule "outputs first" would order visibility and not persistence |

A D1 record whose barrier had not returned when power was lost may be absent afterwards. That is a
legal crash prefix, handled in §8.2. What cannot happen is a *torn* record: §7.2 links a fully written
file, so a reader sees a complete record or no record.

### 7.6 Schema version, the canonical gate, and activation

Schema v4 is exactly v3 plus one `execution` object on the project. No task field changes: in
particular `verification` remains a required non-empty string, and the *resolved* interpretation of it
lives in `plans/` (§9.1), not in a new task field.

```json
"execution": {
  "protocol_version": 1,
  "coordinator_run": null,
  "ownership_generation": 0,
  "attempts": {}
}
```

`research-project enable-execution <project-dir> --expected-revision N` is the only way to create it:

1. It checks the expected revision **first**, before deciding anything else, so a stale caller is told
   it is stale rather than being told the work is already done.
2. It probes the store's filesystem — link-without-clobber, same-volume temporary directory, case
   sensitivity per volume, using a scratch directory it creates inside `execution/` and removes, never
   a declared output path — and refuses with `R-NO-ATOMIC-LINK` or `R-CROSS-VOLUME`.
3. It writes `config.json` with the probe results and every setting of §15.
4. It commits the `execution` block, bumping `schema_version` to 4 and setting `updated` as any commit
   does. `IMMUTABLE_PROJECT_FIELDS` is unaffected: the block is new, not a change to identity [C21].
5. Called again on an already-enabled project whose `config.json` matches, it prints
   `already enabled at generation <G>` and exits zero. Called with a mismatching config, it refuses
   rather than rewriting a generation's settings.

A v3 installation refuses a v4 file outright [C12], which stops an old installation operating on *this*
project. It does not stop that installation operating on a **v3 project in the same workspace against
the same working directory**, and no file in this project can. Hence `R-LEGACY-WRITER`: activation
requires the operator to attest that every installation with write access to this workspace or working
directory is upgraded, the attestation is recorded in `config.json`, and the guarantee is limited to
exactly that. A reader-only installation that understands v4 but implements no executor **must refuse
every mutating command** on a project whose `execution.attempts` is non-empty or whose
`coordinator_run` is not null; reading and validating stay legal.

The volume and link probes are taken once, at activation, and are then treated as facts for the
generation. A volume remounted, a directory moved across filesystems, or a store relocated after
activation is not re-probed and is not detected [UNENFORCED U6].

Closure has its own gate. `research-validate --close` fails unless every attempt in the store is
`RELEASED` or absent, `execution.attempts` is empty, `coordinator_run` is null, and no grant for this
project remains in the registry. Task status alone is not sufficient: a `PREPARED` attempt of a `TODO`
task holds claims, and collecting its journal would erase the evidence that it does.

### 7.7 Retention and collection

Collection removes an attempt's directory only when its label is `RELEASED`, its task is terminal, and
the closure gate above passes. It preserves, for every committed acceptance, the whole chain a reader
needs to interpret the receipt without any hidden file: the `plans/` entry naming the `plan_hash`,
`prepared.json` naming the `definition_hash`, `result.json`, every `sealed` record, `classification`,
every `resolution`, `acceptance`, `commit-observed`, `release`, and each qualifying capture with its
logs truncated to `config.log_cap`. A retained `definition_hash` whose definition was collected
is not auditable, which is why the plan and the contract are retained rather than the hash alone.

## 8. Operations

### 8.1 The operation table

Seventeen operations. Each has a **precondition** over the facts of §6.1 — never over a label — a
**canonical effect** stated as the transitions it commits, and an ordered **durable effect sequence**
whose every prefix §8.2 handles. The canonical column reads `none` when the operation writes only to
the journal or the registry.

Every canonical mutation follows the same three-phase shape, in this order and no other:

```text
intent  ->  acceptance.json, published if absent, carrying expected_revision/run/generation
commit  ->  research-project commit --expected-revision, under .project.lock, fenced per §12.3
ack     ->  commit-observed.json, written only after re-reading project.json and finding the receipt
```

| Id | Operation | Precondition over facts | Canonical effect | Durable effects, in order |
|---|---|---|---|---|
| O1 | `reserve` | task admissible (§10.1); `bound_attempt` is null; no attempt of this task is unreleased | `none` | 1 publish `reservation` |
| O2 | `grant` | `reserved`; not `grant_present` | `none` | 1 under the registry lock: re-scan every grant, verify no conflict (§9.3), insert the grant with `capability` = `none` |
| O3 | `prepare` | `reserved`; `grant_present`; the plan's `plan_hash` still matches `plans/` | `none` | 1 read the baseline digests of every read claim; 2 publish `prepared` |
| O4 | `dispatch` | `prepared`; `authorization_in_force`; `task_status` is `TODO`; `open_causes` empty; capacity (§10.3); the dispatch gate open (§14) | `` `TODO → RUNNING` `` | 1 intent, 2 commit binding `execution.attempts[task_id]`, 3 ack; 4 under the registry lock set `capability` = `issued` with a fresh `capability_id`; 5 call the adapter's `start`; 6 publish `launch` with the outcome; 7 under the registry lock set `capability` = `consumed` |
| O5 | `record-result` | `launched` or `uncertain_start`; written by the worker or by the inline executor | `none` | 1 fsync every declared output and its parent; 2 publish `result` |
| O6 | `seal` | the adapter reports the scope's process tree exited and the host cannot resume it | `none` | 1 publish `sealed/<scope-id>` |
| O7 | `classify` | `sealed_scopes` contains `worker`; `result` non-null, or `stop_evidence` non-null, or the deadline passed | `none` | 1 validate every capture; 2 publish `classification` |
| O8 | `hold` | a cause of §7.4's `cause_class` is detected; no other precondition, and none on the label | `none` | 1 publish `holds/<cause-id>` |
| O9 | `resolve` | `open_causes` contains this `cause_id`; a `human_review` cause resolves only on an assessment meeting §11.5 | `none` | 1 publish `resolutions/<cause-id>` |
| O10 | `run-check` | `prepared`; the grant holds the check's declared read **and** write claims; scope `check:<id>` is in `declared_scopes` | `none` | 1 publish the capture; 2 publish `sealed/check:<id>` |
| O11 | `accept` | `open_causes` empty; `open_scopes` empty; `classified`; adequacy holds (§11.4); every qualifying capture's after-snapshot equals the `accepted_snapshot` (§11.3) | `none` | 1 publish `acceptance` |
| O12 | `commit-acceptance` | `accepted`; `revision`, `run`, `generation` and the re-derived `definition_hash` all still equal the acceptance's | `` `RUNNING → DONE` ``, `` `RUNNING → BLOCKED` ``, or `` `RUNNING → SKIPPED` `` | 1 append the receipt to `evidence.md`; 2 commit the complete candidate (§8.4); 3 ack |
| O13 | `withdraw` | `open_causes` contains a cause resolved `withdraw`, or authorization is no longer in force; `open_scopes` empty | `` `RUNNING → TODO` ``, `` `RUNNING → BLOCKED` ``, or `none` | 1 publish `resolutions/<cause-id>`; 2 publish `acceptance` with `intent` `TODO` or `BLOCKED`; 3 commit; 4 ack |
| O14 | `retry` | `task_status` is `BLOCKED`; `open_causes` empty; every attempt of this task released | `` `BLOCKED → TODO` `` | 1 publish `acceptance` with `intent` `TODO` on a new attempt; 2 commit, clearing `block_reason`; 3 ack |
| O15 | `stop` | an operator asked, or the deadline passed | `none` | 1 publish `holds/<cause-id>` with `cause_class` `operator_stop`; 2 call the adapter's `terminate` for every open scope; 3 publish `stop-evidence` naming the scopes it covers; 4 O6 for each scope the evidence establishes |
| O16 | `release` | `sealed_scopes` equals `declared_scopes`; `launch_capability` is `consumed` or `revoked`; `open_causes` empty; `accepted` implies `commit_observed`; not `indeterminate` | `none` | 1 publish `release` with `grant_removed` false; 2 under the registry lock remove the grant; 3 publish nothing further — step 1 is the authorization, step 2 the act |
| O17 | `take-over` | the predecessor is proven dead (§12.2) | `none` | 1 commit the new `coordinator_run` and `ownership_generation`; 2 delete `runtime/`; 3 reproject every attempt; 4 for each, resume at §8.2's completion for its prefix |

Three ordering questions revision 4 answered twice, answered once here.

- **Release order.** `release` is published **before** the grant is removed, and its `grant_removed`
  field is therefore false in the record. The registry is the authority for admission — an unremoved
  grant keeps excluding writers, which is the safe direction — and the journal is the authority for
  *authorization* to remove it. A projection that finds `release` present and the grant still there
  reports the grant as held and completes step 2; it never reports the claim as free.
- **Acquisition order.** `reservation` precedes the grant, so a grant with no `prepared` is always
  reconcilable: the reservation names the task, the plan hash, the claims, and the counter. A
  reservation with no grant holds nothing. Both prefixes appear in §8.2.
- **Intent before commit, everywhere.** Withdrawal, acceptance, retry and disposal all use the same
  three-phase shape, so no operation orders its journal record after its commit and no crash leaves a
  canonical status whose journal has no explanation.

### 8.2 Crash prefixes and their completions

A crash prefix is a proper prefix of an operation's durable effect sequence. Recovery (§13) enumerates
active attempts, computes facts, and for each prefix below performs the completion — which is itself an
operation, so a crash during recovery is just another prefix.

| Operation | Prefix | What the store shows | Completion |
|---|---|---|---|
| O1 | after 1 | `reserved`, no grant | O2, or abandon by publishing `release` with `receipt` null and `sealed_scopes` empty |
| O2 | after 1 | grant, no `prepared` | O3 if the plan still matches; else O16 |
| O3 | between 1 and 2 | grant, no `prepared`; baseline read may have failed | identical to the O2 prefix: O3 is retried from the reservation, and a second baseline read is legal because nothing has been dispatched |
| O4 | after 1 | `acceptance` with `intent` `RUNNING`, nothing committed | re-run step 2; the intent is idempotent by `receipt_id` |
| O4 | after 2 | canonical `RUNNING`, no `commit-observed`, `capability` `none` | re-read `project.json`; if the binding is present, ack and continue at step 4 |
| O4 | after 4 | `capability` `issued`, no `launch` | **ambiguous**: the adapter may or may not have started. Publish `launch` with `start_outcome` `ambiguous`, set `uncertain_start`, and go to §13.2. Never re-issue the capability |
| O4 | after 5, before 6 | the adapter started something; nothing recorded | the same ambiguous path; the handle is unknown, so §13.2's discovery is the only route |
| O4 | after 6 | `launch` present, `capability` still `issued` | set `capability` `consumed` under the registry lock; the fact was already durable |
| O5 | after 1 | outputs on disk, no `result` | wait for the worker, or on stop evidence classify from what is there (§13.3) |
| O7–O11 | any | one journal record missing | re-run the operation; publish-if-absent makes a re-run either `created` or `identical` |
| O12 | after 1 | receipt in `evidence.md`, not in `project.json` | not proof of anything (I4): re-run step 2. The evidence append is deduplicated by `receipt_id`, so it is written at most once |
| O12 | after 2 | committed, no `commit-observed` | re-read `project.json`; if the receipt tuple is at `evidence_index`, ack. If the revision moved on and the receipt is absent, the commit did not land: re-check O12's precondition and retry |
| O13, O14 | any | as O12 | identical, because all three share the intent/commit/ack shape |
| O15 | after 1 | `operator_stop` cause, scopes open | call `terminate` again; it is idempotent by handle |
| O15 | after 3 | stop evidence, unsealed scopes | O6 for each scope the evidence covers; a scope the evidence does not cover stays open, and O16 stays blocked |
| O16 | after 1 | `release` present, grant present | remove the grant. This is the only prefix in which the journal is ahead of the registry, and it is safe in that direction |
| O17 | after 1 | new generation, old `runtime/` | continue at step 2 |

### 8.3 Replay and idempotence

- Every D1 publication is idempotent by name: a re-run returns `created` or `identical`, and
  `identical` completes the barrier (§7.2).
- Every canonical mutation is idempotent by **receipt identity**, never by status equality. `TODO` is
  not proof that *this* attempt's withdrawal was applied — a later retry of the same task produces the
  same status — so O12, O13 and O14 all check for their own `receipt_id` at `evidence_index` in the
  task's `evidence` array, and compare `bound_attempt` to their own `attempt_id`, before concluding the
  mutation landed.
- The evidence append of O12 step 1 is deduplicated on `receipt_id`, so an interrupted commit followed
  by a retry appends once.
- Adapter operations are idempotent by handle: `terminate` on an exited scope succeeds, `observe` is
  read-only, and `start` is **never** retried, because a retried `start` is exactly the second
  executable write authority I1 forbids.

### 8.4 The canonical candidate

Every canonical mutation builds a **complete** candidate from the loaded revision through one shared
builder, so no operation invents a field rule of its own. The builder sets, for the task it mutates:

- `status` to the target, which must be in `TASK_TRANSITIONS[current]` [C20];
- `block_reason` to a non-empty string when the target is `BLOCKED` and to null otherwise [C27];
- `skip_reason` to a non-empty string when the target is `SKIPPED` and to null otherwise;
- `authorization` to `{required, status, scope, source, authorized_at}` where withdrawing consent sets
  `status` to `pending` when `required` is true and `not_required` when it is false, and sets **both**
  `source` and `authorized_at` to null, because the validator rejects retaining them under any status
  but `explicit` [C23];
- `evidence` extended by one entry naming the receipt (§11.3), never replaced;
- and nothing else on the task. `TASK_FIELDS` has no `notes` key [C28], so a withdrawn consent's
  history is preserved in the appended evidence entry and in the attempt's immutable `resolution`
  record — not in an invented field that validation would reject.

At project level the builder sets `current_tasks` to the exact set of `RUNNING` task ids [C1], adds or
removes `execution.attempts[task_id]` to match the binding the mutation intends, and leaves
`IMMUTABLE_PROJECT_FIELDS` alone [C21]. The candidate is then dry-run through the real validator before
it is committed; §21.2 requires the model to do exactly that rather than to check enum membership.

## 9. Resources and claims

### 9.1 The execution plan

A task's `verification` is a required non-empty free-text string [C31], and its evidence is
referenced through the canonical evidence object [C13]. A coordinator
cannot resolve free text into commands twice and call the result stable. So resolution happens **once**,
and its output is a durable, immutable, versioned artifact:

```text
execution/plans/<task-id>.<plan-hash>.json
```

The plan is written by the coordinator at admission, before any reservation, and `plan_hash` is the
`sha256` of its canonical serialization minus the hash field. It carries:

| Field | Meaning |
|---|---|
| `task_id`, `source_revision` | the task and the canonical revision the plan was resolved from |
| `verification_text` | the exact `verification` string it interprets, so a later reader can see what was interpreted |
| `checks[]` | ordered; each `{check_id, kind, argv or criteria, cwd, expect_exit, subjects[], reads[], writes[], executor, mutation_policy}` |
| `reads[]` | every path the **task** reads, beyond its dependencies' outputs |
| `writes[]` | every path the task writes, which must equal the union of its declared `outputs` |
| `enumerable` | bool; false means `R-UNENUMERABLE` and inline execution |
| `definition_hash` | see below |

`definition_hash` is defined in exactly one place, here, as the `sha256` of the canonical serialization
of this object and nothing else:

```json
{"authorization": {"required": true, "scope": "..."},
 "checks": [...],
 "cwd": "<the resolved absolute working directory>",
 "depends_on": ["..."],
 "effect": {"kind": "local_write"},
 "outputs": [{"root": "target", "path": "...", "required": true}],
 "reads": ["..."],
 "success_criteria": "...",
 "task_id": "...",
 "verification_text": "...",
 "writes": ["..."]}
```

Three fields in it are there because revision 4 omitted them and each can change what an identical task
description means: the resolved `cwd` (identical relative paths denote different files under a different
working directory), the resolved `checks` array (which is what `verification_text` was interpreted to
mean), and `outputs[].required`, which the canonical output object requires [C26] and which decides
whether a missing output is a failure. Attempt identity (`attempt_id`), the baseline snapshot, and host
capabilities are deliberately **not** in it: they change between attempts of the same definition.

A check is resolved from text to `argv` only where the split preserves meaning. A `&&`, `||`, pipeline,
redirection, or a `;` outside quotes is **not** split into several unconditional checks — that changes
the semantics of the command — it is either kept whole and executed through an explicit shell check
(`kind: shell`, with the shell named in `argv[0]`), or the plan is marked not enumerable. A literal `|`
or `;` inside a quoted argument is not composition and never triggers either path.

### 9.2 Claim namespaces

A claim is a string in one of three namespaces. `REFERENCE_ROOTS` decides which of them an output can
map to at all [C11].

| Namespace | Form | Covers |
|---|---|---|
| local | `local:<absolute resolved path>` | one regular file, or one directory *as an opaque unit* when and only when §22's directory semantics land; until then a directory subject is `R-DIRECTORY-SUBJECT` |
| store | `store:<attempt-id>` | the attempt's own subtree of the execution store: its captures, logs, result and heartbeat. Implicitly granted by O2 to that attempt alone, so a worker writing its own manifest is not violating its claim set |
| external | refused | `external` outputs are URL-like references, not paths [C11]; no rule here maps them to a claim, and `R-EXTERNAL-REFERENCE` sends the task inline where its effect receipt belongs |

Local paths are canonicalized before comparison: made absolute against the plan's `cwd`, symlinks
resolved, `.` and `..` removed, and — on a case-insensitive volume, per `config.json`'s per-volume probe
— compared case-insensitively. A path containing a symlink is rejected at admission, and
canonicalization happens exactly once, at preparation; neither stops an outside actor retargeting a
symlink afterwards, so two claims that were distinct at preparation can name one file at run time
[UNENFORCED U2].

### 9.3 The conflict relation

Two claims conflict when one is a write and they name the same path, or when one is a write and the
other names an **ancestor or descendant** of it. The relation is **symmetric**: `local:/a/b` as a write
conflicts with `local:/a` as a read, and `local:/a` as a write conflicts with `local:/a/b` as a read.
Revision 4 called this one-directional while giving both examples; it is not.

Read–read never conflicts. Two attempts conflict when any claim of one conflicts with any claim of the
other. An attempt's own claims never conflict with themselves, which is what lets one attempt hold both
a task write and a check read on the same file.

The relation is computed over **declared** claims. That is exactly as strong as the declarations, which
is why v1 refuses the shapes whose declarations cannot be complete (`R-UNENUMERABLE`) rather than
comparing partial claim sets and calling the comparison prevention. The residual is that a write to a path
nobody declared conflicts with nothing, is granted by nothing, and is seen by nothing
[UNENFORCED U1]:
nothing in this protocol prevents an undeclared write.

Three derivations that revision 4 got wrong or omitted:

- **A repository operation that changes the working tree claims the working tree.** `git switch`,
  `git rebase` and `git checkout` update the index and the working tree, not only `.git`, and `.git` and
  `src/a.py` are siblings, so a `.git` write claim does not conflict with a scoped writer. Such a task
  claims `local:<repository root>` as a write, which conflicts with every path under it, or it is not
  enumerable and runs inline.
- **Every check declares its writes.** A test runner writing `__pycache__`, a linter writing a cache, a
  build writing artifacts: each is a `writes[]` entry on that check, part of the attempt's grant, and
  part of the conflict relation. A check whose writes cannot be listed makes the plan not enumerable.
- **Protocol-owned paths are reserved.** `project.json`, `spec.md`, `evidence.md`, `briefing.md`,
  `reflection.md`, `INDEX.md`, `MEMORY.md`, the `execution/` subtree, and the registry are
  coordinator-only. Derivation **rejects** a worker claim naming any of them, any ancestor of any of
  them, or any descendant of `execution/` outside that attempt's own `store:` namespace. A task whose
  declared output is one of these is refused at admission rather than handed to a worker with
  contradictory instructions; the coordinator writes them itself.

### 9.4 Baselines and produced sets

`prepared.baseline` maps every read claim to the digest of its contents, or `absent`. It is immutable
for the attempt's life and is the reference for §11.3's binding and §13.3's reconciliation. A dependency
output that this task also writes is **not** in the read-only baseline: it appears in `writes[]`, and
including it in both would assert an unchanging file the task is licensed to change.

`result.produced` maps every declared output claim to a digest or `absent`. Comparison against the
baseline and the accepted snapshot yields a **tagged** outcome per path, never one of three assumed
digest cases:

| Tag | Meaning |
|---|---|
| `created` | `absent` before, digest after |
| `modified` | digest before, different digest after |
| `unchanged` | same digest before and after |
| `still_absent` | `absent` before and after |
| `deleted` | digest before, `absent` after |
| `wrong_type` | the path exists and is not a regular file |
| `unreadable` | the path exists and cannot be read; this is a `corrupt_record` cause, not an absence |

`still_absent` for a `required: true` output is a failure; for `required: false` it is legal. `deleted`,
`wrong_type` and `unreadable` all raise causes rather than being folded into a digest comparison.

## 10. Scheduling

### 10.1 Admission

Admission is the gate O1 (`reserve`) tests. It is a conjunction, and every clause is checked against
canonical state or the plan — never against a label:

1. The project's `status` is `EXECUTING`. A task may be `TODO` in a project that is `PLANNING`,
   `ALIGNING`, `BLOCKED` or `DONE`; none of those may dispatch. Revision 4 tested the task and not the
   project, which admitted work from a project the user had not yet released.
2. `execution.protocol_version` is present and understood, and `ownership_generation` matches this
   coordinator's (§12.1). A coordinator that does not own the project admits nothing.
3. `task_status` is `TODO`, and the task has no unreleased attempt (`bound_attempt` null and no
   attempt directory whose label is other than `RELEASED`).
4. Every id in `depends_on` is `DONE`. `SKIPPED` does not satisfy a dependency: a skipped task
   produced no outputs, so a dependant's baseline would reference files nothing wrote. `BLOCKED` and
   `TODO` obviously do not satisfy it.
5. `authorization_in_force` — the task's `authorization.status` is `not_required`, or it is `explicit`
   with a non-null `source` and `authorized_at` [C23]. `pending` does not dispatch.
6. The task's `effect.kind` is `none` or `local_write`, and its `effect.confined_to` covers every
   declared write claim (§9.2). Otherwise `R-EFFECT-NOT-CONFINED`.
7. The plan is enumerable: every check's command decomposes under §9.1's rule and every subject,
   read and write claim is a concrete path. Otherwise `R-UNENUMERABLE`, and the task runs inline.
8. Every `required: true` output is a `subject` of at least one check. Otherwise `R-CHECK-UNCOVERED`,
   and the task does not run at all.
9. No claim conflicts (§9.3) with a claim held by any attempt that is not `RELEASED`, including
   attempts of other projects sharing the workspace, whose claims are read from their grants (§10.4).
10. Capacity permits one more attempt (§10.3).

Clauses 1–8 are properties of the project and the plan and are stable for the scan; clauses 9 and 10
are properties of the world and are re-tested under the registry lock at grant time (O2), because
between the scan and the grant another executor may have taken the resource. **The scan is advisory;
the grant is authoritative.** A design that treats the scan as the decision has a race whose window is
exactly the time the coordinator spends preparing.

Admission produces a refusal code, not silence. A task that fails clause 7, 8 or 6 is reported with
its code at the moment it is considered, so an operator reading the console sees `R-UNENUMERABLE` next
to the task that caused it rather than inferring it from a serialized run.

### 10.2 The ready loop

```
loop:
  facts   = project(every unreleased attempt)                    # §6.1
  settle  = attempts with a completable prefix                   # §8.2
  if settle: complete one, continue                              # progress beats new work
  if stop_requested and no attempt is RUNNING: close             # §13
  ready   = tasks satisfying §10.1 clauses 1..8, in plan order
  ready   = filter(ready, no conflict with held claims)          # advisory, clause 9
  while capacity_free() and ready and preparing() < max_prepare:
      t = ready.pop(0)
      O1 reserve(t); O2 grant(t)   # clause 9 and 10 re-tested under the registry lock
      O3 prepare(t); O4 dispatch(t)
  if nothing changed this pass: wait(poll_interval)              # §15
```

Four rules make this deterministic enough to test.

- **Settling beats starting.** An attempt with a completable crash prefix, a delivered result, or a
  sealed scope is progressed before any new task is considered. Otherwise a coordinator at capacity
  can spin: it has nothing to start and never looks at what it could finish.
- **Plan order is the tie-break, and the only one.** `ready` is ordered by the task's index in
  `project.json`'s `tasks` array — the order the plan was written in. No critical-path heuristic, no
  dependant count, no duration estimate: §3.2 measured a median parallel width of 1.42 and a maximum
  of 3.87, where the difference between orderings is unmeasurable, and an unmeasurable heuristic is a
  source of nondeterminism in the model for no gain. Fairness is a consequence: a task cannot be
  passed over twice while a later one starts, because the list is rebuilt in the same order each pass.
- **`max_prepare` bounds in-flight preparations** at 2 (§15). Preparation snapshots a baseline
  (§9.4), which reads every declared input; a coordinator that prepares eight tasks to fill four
  slots does that work for four it discards.
- **A result arriving between the scan and the launch does not cancel the launch.** The scan's facts
  are stale by construction. If a worker's `result` lands while the coordinator is inside O4, the
  launch completes — the capability was already issued and re-issuing or abandoning it violates
  §8.2's ambiguity rule — and the result is settled on the next pass. What the coordinator must not
  do is re-derive `facts` mid-operation and take a different branch: an operation's preconditions are
  evaluated once, at its start, and its durable effects then run to completion or leave a prefix.

### 10.3 Capacity

`capacity_free()` counts, against `max_concurrent`, whose default is 4 (§15.1):

- every attempt whose label is `RESERVED`, `PREPARED`, `DISPATCHING`, `RUNNING`, `PUBLISHED`,
  `UNCERTAIN`, `CLASSIFIED`, `HELD` or `QUARANTINED` — that is, every attempt that is neither
  `RELEASED` nor absent. A reserved attempt holds claims and will consume a slot; an `UNCERTAIN` one
  may have a live process. Counting only `RUNNING` would let a coordinator hold twelve attempts and
  four running processes.
- the coordinator's own inline work as one. When the coordinator runs a task inline (capacity one,
  `R-UNENUMERABLE`, `R-NO-ADAPTER`, `R-DIRECTORY-SUBJECT`), that task is an attempt with a grant like
  any other, so it is already in the count above; the rule is stated because a design that treats
  inline work as free reaches five concurrent processes at a bound of four.

The bound is 4 and it is a **configured setting with a measured justification, not a tuned optimum**:
§3.2 found maximum level width 3.87 across the sampled graphs, so a bound above 4 cannot be exercised
by the workload that motivated this design, and a bound of 4 is never the binding constraint in that
sample. A grant refused for capacity is `R-CAPACITY`, the one refusal that resolves itself: the task keeps its
place in plan order and is re-scanned on the next pass. The bound is stored in `config.json` (§15) so
raising it is a recorded operator decision rather than a code change. Nothing in this document claims 4 is optimal; §21.3 is where a different number would
have to be earned.

### 10.4 The execution registry

The registry is the authority for I1. It lives at `<workspace-root>/.execution-registry/`:

```
.execution-registry/
├── registry.lock/                 # DirectoryLock [C24], `registry_lock_timeout` 5.0s
└── grants/<attempt-id>.json       # one file per unreleased attempt
```

A grant records `attempt_id`, `project_id`, `task_id`, `coordinator_run`, `ownership_generation`,
`claims` (the resolved write and read claim sets of §9.2), `capability` (`none` | `issued` |
`consumed` | `revoked`), `capability_id` (null unless `capability` is `issued` or `consumed`),
`granted_at`, and `body_digest`.

Four properties:

- **It is per workspace, not per project.** Two projects in one workspace can declare the same
  `local:` path — a shared repository is the ordinary case — so a per-project registry would enforce
  nothing across the boundary that matters most.
- **Scan-and-insert is one critical section.** O2 takes `registry.lock`, reads every grant, re-tests
  §10.1 clauses 9 and 10 against them, writes the new grant, and releases. A design that reads the
  registry, decides, and then writes has the race the lock exists to remove.
- **Removal is also under the lock**, and it happens in O16 (`release`) *after* the `release` record
  is published (§8.1). The registry is the admission authority; the journal is the release
  authorization. A crash between the two leaves a grant with a published `release`, which the next
  scan removes; the reverse order would leave an attempt with no grant and no record that it ended.
- **A grant is not a lease and does not expire.** There is no timeout after which a grant is
  presumed abandoned, because a presumption is exactly the ambiguity I1 exists to exclude. A grant
  whose coordinator is gone is removed by O17 (`take-over`), which requires proof of death (§12.2), or
  by an operator. Absent both, the project stays stopped: `R-LIVE-PREDECESSOR`.

## 11. Verification and acceptance

### 11.1 Resolved checks

A task's `verification` field is a required non-empty free-text string [C31], and it stays one: v4
does not change its
type or make it structured. What changes is that at admission the coordinator **resolves** it once,
into the `checks[]` array of the plan (§9.1), and the plan — not the prose — is what executes.

Resolution is mechanical and refusing:

- A line that is a runnable command with no shell composition (§9.1) becomes a check with
  `kind: command`, an `argv` array, a `cwd`, an `expect_exit` (default `0`), and declared
  `subjects[]`, `reads[]`, `writes[]`.
- A line that describes a property to judge rather than a command to run becomes a check with
  `kind: criteria` and a `criteria` string. It has no `argv` and no exit status; it is assessed
  (§11.5), and its `subjects[]` are the paths the judgement reads.
- Anything else — shell composition, an unresolvable path, a subject that is a directory — refuses:
  `R-UNENUMERABLE` or `R-DIRECTORY-SUBJECT`. The task runs inline, where the coordinator's own
  judgement is available and no claim set has to be exact.

The resolution is recorded once, in `plans/<task-id>.<plan-hash>.json`, and contributes to
`definition_hash` through `verification_text` and `checks` (§9.1). A task whose prose is edited after
admission has a different `definition_hash`, and §11.3 refuses to accept against it.

### 11.2 Capture records

Every check execution produces exactly one `capture` record at
`attempts/<attempt-id>/captures/<check-id>/<NNNN>-<capture-id>.json`. `NNNN` is the run sequence
(§7.3); a rerun appends, never replaces, so the record of a flaky check is the whole sequence.

| Field | Type | Meaning |
|---|---|---|
| `check_id` | string | the check in the plan |
| `sequence` | integer | 1 for the first run of this check in this attempt |
| `kind` | `command` \| `criteria` | copied from the plan |
| `argv` | array of string, or null | null for `criteria` |
| `cwd` | string | resolved absolute path |
| `started_at`, `ended_at` | RFC 3339 UTC | wall clock, for §15 only |
| `exit` | object | the exit-status variant below; null for `criteria` |
| `stdout_ref`, `stderr_ref` | string or null | `logs/<check-id>/<NNNN>.out` / `.err`, or null when the stream was empty |
| `stdout_bytes`, `stderr_bytes` | integer | byte counts of what was captured |
| `stdout_truncated`, `stderr_truncated` | bool | true when the cap of §15 was hit |
| `subjects` | ordered array | one entry per subject: `{claim, before, after}`, each digest or `absent` or `wrong_type` or `unreadable` (§9.4) |
| `assessor` | object or null | who judged this capture: `{kind, identity}` where `kind` is `exit_status`, `coordinator` or `human`; null while unassessed |
| `criteria` | string or null | for `kind: criteria`, the resolved criteria text that was judged |
| `rationale` | string or null | for a judged capture, why. Required when `assessor.kind` is `coordinator` or `human` |
| `adequate` | bool or null | the adequacy judgement of §11.4; null while unassessed |

`exit` is a tagged variant, because "the exit code" is not a total function on a process. The shipped
`record-evidence` path already runs its command with `shell=False` and no shell interpretation [C14] and
already raises rather than returning a code when the timeout fires [C15]; the variant below is that
distinction made explicit in the record instead of collapsed into an integer:

| Variant | Fields | Raised when |
|---|---|---|
| `exited` | `code` (integer 0–255) | the process exited normally |
| `signalled` | `signal` (string, e.g. `SIGKILL`), `core` (bool) | the process was killed |
| `timeout` | `limit_seconds` | the check's own `check_timeout` fired; the process was terminated |
| `spawn_failed` | `errno` (string), `detail` | the process never started — `ENOENT`, `EACCES`, `E2BIG` |
| `unknown` | `detail` | the adapter reported completion without a status |

Only `exited` with `code == expect_exit` can satisfy a `command` check. `signalled`, `timeout` and
`unknown` are **not** failures of the check: they are `indeterminate_check` causes (§14), because a
killed process says nothing about the property being checked. `spawn_failed` *is* a failure of the
plan, and raises a `plan_defect` cause rather than being retried: nothing about the environment will
make a missing executable appear.

The three field names above (`assessor`, `criteria`, `rationale`) are the schema. Revision 4 wrote a
single free `verdict` string, which made "the check passed" and "someone decided the check passed"
the same record, so an unassessed capture and a human-approved one were indistinguishable.

### 11.3 Binding evidence to the bytes that were checked

I3 says an acceptance binds the current task definition to the bytes that were checked. Three things
make that concrete.

**The accepted snapshot.** O11 (`accept`) computes `accepted_snapshot`: the ordered map from every
declared write claim and every check subject to its digest (or `absent`) *at acceptance time*. It then
requires that for every **qualifying** capture — the highest-sequence capture of each check, the one
whose adequacy is being relied on — that capture's `subjects[].after` equals the corresponding entry
in `accepted_snapshot`. If a file changed after the check ran, acceptance refuses; the difference is a
`stale_evidence` cause, and the check reruns (§11.4).

**The definition hash.** `accept` also re-derives `definition_hash` from the *current* `project.json`
and compares it to the plan's. An edit to the task's `verification`, `outputs`, `success_criteria`,
`depends_on`, `effect` or `authorization` between dispatch and acceptance changes the hash, and
acceptance refuses with a `definition_changed` cause. The attempt is withdrawn (O13) rather than
accepted: it verified a task that no longer exists. Nothing prevents the edit — the definition lives in
`project.json`, which a person may change at any time — so this is detection at the last safe moment,
not prevention [UNENFORCED U3].

**The receipt.** The receipt is the only proof a commit landed (I4). It is
`rcp-` + 32 hex characters of the sha256 of the canonically serialized object

```json
{"accepted_snapshot": …, "attempt_id": …, "definition_hash": …, "intent": …, "qualifying_captures": …}
```

with `qualifying_captures` the ordered list of `capture_id`s relied on. The receipt appears in three
places and means the same thing in each: in `acceptance.json` as the intent, in the task's `evidence`
array as the canonical record, and in `commit-observed.json` as the confirmation that a re-read of
`project.json` found it. Nothing infers a commit from a status: a `DONE` task whose `evidence` lacks
the receipt is a task whose acceptance did not land, whatever its status says.

The expected-exit policy replaces revision 4's free verdict: a `command` check is satisfied when
`exit` is `exited` and `code == expect_exit`, and by nothing else. `expect_exit` is declared in the
plan, so a check that is *meant* to fail (a negative test, a `grep -q` that must find nothing)
declares its expectation instead of relying on prose. An assessor cannot override an exit-status
mismatch into a pass; it can only raise a `human_review` cause and resolve it (§11.5), which is a
recorded decision with an identity attached.

### 11.4 Adequacy and the rerun cap

`adequate` is per capture; the attempt's outcome is a function of the qualifying captures.

| Check kind | Capture state | `adequate` |
|---|---|---|
| `command` | `exit` = `exited`, `code` = `expect_exit` | true |
| `command` | `exit` = `exited`, `code` ≠ `expect_exit` | false — the check failed |
| `command` | `exit` = `signalled` \| `timeout` \| `unknown` | null — `indeterminate_check` cause |
| `command` | `exit` = `spawn_failed` | null — `plan_defect` cause |
| `command` | any, but a subject is `unreadable` or `wrong_type` | null — `corrupt_record` cause |
| `criteria` | assessed true by a permitted assessor with a `rationale` | true |
| `criteria` | assessed false by a permitted assessor with a `rationale` | false |
| `criteria` | unassessed | null — the attempt cannot be accepted |
| either | no capture at all | null — the check did not run; the attempt cannot be accepted |

A capture with an empty `stdout` and empty `stderr` is **not** thereby inadequate, and not thereby
adequate: adequacy is the exit status for a `command` check, and a silent successful command is the
normal case. Revision 4's model asserted the opposite for empty-field commands and the review was
right to call it out.

The attempt's `canonical_intent` follows from the qualifying captures:

- every qualifying capture `adequate: true`, and at least one check exists → `DONE`;
- any qualifying capture `adequate: false` → the failure path of §14, whose classification decides
  between retry, `BLOCKED` and stop;
- any qualifying capture `null` → not acceptable; the cause is open and O11's precondition fails.

**A task with no checks cannot reach `DONE`.** Clause 8 of §10.1 refuses admission when a
`required: true` output has no covering check, and a task with no `required` outputs and no checks has
nothing to verify — it is refused as `R-CHECK-UNCOVERED` rather than accepted by vacuous truth.
Revision 4's model computed `DONE` from an empty check list, which is the same defect as trusting an
empty test suite.

**The rerun cap, `max_reruns`, is 16 per check per attempt.** A check may rerun on a `stale_evidence` or
`indeterminate_check` cause; each rerun is a new capture with the next `sequence`. At 16, the
coordinator stops rerunning and raises a `flaky_check` cause requiring resolution (§14). The cap is
what makes §7.3's four-digit sequence unreachable at its bound and what stops an
always-indeterminate check from spinning a coordinator forever. It is in `config.json` (§15).

### 11.5 Who may assess

A `criteria` check is a judgement, so the record must say whose.

- `assessor.kind: exit_status` is not a judgement and carries no identity: it is the mechanical rule
  of §11.3 applied to a `command` capture. It never appears on a `criteria` capture.
- `assessor.kind: coordinator` carries `identity` = the coordinator's `coordinator_run`. It is
  permitted **only when the coordinator did not produce the bytes being judged** — that is, when the
  attempt's `writer` for the subject claims is a worker, not the coordinator itself. The comparison is
  between `assessor.identity` and the producing scope's identity as recorded in `launch.json` and
  `result.json`, not between the strings "coordinator" and "worker": an inline attempt has the
  coordinator as producer, and there the coordinator may not also assess.
- `assessor.kind: human` carries `identity` = the `source` recorded for the resolution, in the same
  form authorization uses [C23]. It is required for an inline attempt's `criteria` checks, and for
  any `human_review` cause.

So the rule is one sentence: **the producer may not be the assessor.** Where they coincide — inline
execution, capacity one, a refused host — the `criteria` checks of that task are not
self-assessable, and the attempt holds a `human_review` cause until an operator resolves it. This is
a real restriction and it is the honest one: a coordinator that judges its own output has produced a
record of its own opinion, not a verification. §22 defers the alternative (a second independent
assessor process) rather than pretending the coincidence is harmless.

A worker never assesses. It captures — it runs the command, records the exit variant, digests the
subjects, and publishes. `assessor` is null in every capture a worker writes, and the coordinator
fills it in, which is why `capture` records are worker-written and `classification` is
coordinator-written (§7.4).

## 12. Ownership, takeover, and fencing

### 12.1 Run identity

Three identifiers, with three different lifetimes:

| Identifier | Where | Changes when | Purpose |
|---|---|---|---|
| `coordinator_run` | `project.json.execution`, every record | a coordinator process starts and takes ownership | says *who* wrote a record |
| `ownership_generation` | `project.json.execution`, every record | ownership changes hands (O17), monotonically | fences stale writers |
| `attempt_id` | the attempt directory, every record | every reservation | says *what* a record is about |

At startup a coordinator publishes `owners/<coordinator-run>.json` before its first mutating
operation. It records `host_id` (a stable machine identifier), `boot_id` (the current boot's
identifier), `pid`, `process_start` (the process's own start time as the OS reports it), `started_at`,
and the `generation` it holds. This record exists for one reason: it is the evidence a successor needs
in §12.2. Without it a successor has a `coordinator_run` string and no way to ask whether the process
behind it is alive, which is how "the heartbeat is stale" becomes a takeover.

`owners/` survives takeover. It is the one part of the store outside `attempts/` that the retention
policy (§7.7) keeps for the project's life, because a generation's records can only be explained by
reference to the owner that wrote them (§6.3).

### 12.2 Takeover requires proof of death

O17 is the only operation that raises `ownership_generation`, and its precondition is a proof, not an
inference. Exactly three proofs are accepted, and each is recorded in the new owner record's
`took_over_from` object:

| Proof | Established by | Why it is a proof |
|---|---|---|
| `host_rebooted` | the predecessor's `owner` record has this `host_id` and a **different** `boot_id` | every process from the previous boot is gone, including descendants |
| `pid_gone` | same `host_id`, same `boot_id`, and no live process with the predecessor's `pid` **and** matching `process_start` | the pid is not merely absent but not reused; a matching start time would mean the process is alive |
| `operator_attestation` | a recorded human attestation naming the predecessor's `coordinator_run`, with a `source` and an `attested_at`, in the form authorization uses [C23] | a person has taken responsibility for the claim |

Nothing else is a proof. In particular:

- **A stale heartbeat is not a proof.** Heartbeats are D2 (§7.5) and advisory (§6.1); a paused, swapped,
  or slow process stops heartbeating and keeps running. Revision 4 permitted takeover on staleness,
  which makes two coordinators with valid grants the *expected* outcome of a long GC pause.
- **A different host is not a proof.** A successor on host B can say nothing about a process on host A.
  `host_id` mismatch alone yields `R-LIVE-PREDECESSOR`.
- **An unconsumed launch capability blocks takeover regardless.** If any grant has
  `capability: issued`, the predecessor may have called `start` and not recorded the outcome
  (§8.2, O4 after 4). Takeover then requires a proof from the table *and* revokes the capability
  under the registry lock before any dispatch — `capability: revoked`, never back to `none`, so the
  journal keeps saying that a start may have happened.

When no proof is available, the coordinator refuses: it does not take over, it does not remove grants,
it does not dispatch, and it reports `R-LIVE-PREDECESSOR` naming the predecessor's `coordinator_run`,
`host_id` and `pid`, and the one operator action that resolves it (attest, or bring the host down).
The project stays stopped. This is the narrowing the review asked for: v1 recovers from a crash on the
same host and from a reboot, and refuses everything else visibly. §22 defers takeover of a live
dispatcher.

Takeover of an attempt whose scopes are not sealed is a separate matter and is *not* unblocked by a
death proof. The predecessor's process being dead does not seal its children (I2). Since the envelope
(§3.1) forbids detached and resumable execution, a dead coordinator's worker subprocesses are dead
too — but that is a property of the adapter, so it is the adapter that must attest it (§17), and an
adapter that cannot yields `R-DETACHED-CHILD` for asynchronous dispatch in the first place. The
attempt is sealed via §13.1, or held.

### 12.3 Fencing canonical mutations

Every canonical mutation is a `research-project commit` and carries three fences, all checked inside
`.project.lock` — acquired with `project_lock_timeout` [C29] — by the committing code path:

1. `--expected-revision <r>`, which the existing command already enforces [C1].
2. `expected_run`: the candidate's `execution.coordinator_run` must equal the on-disk value the
   coordinator read. A coordinator whose run was superseded fails here.
3. `expected_generation`: the candidate's `execution.ownership_generation` must equal the on-disk
   value. A generation that has advanced means someone took over, so this commit is a ghost's.

Revision and generation are independent: a takeover that changes nothing else still raises the
generation, so a stale coordinator holding a correct revision is still fenced. The check is inside the
lock because a check outside it is a check against a value that may change before the write — the same
reason `commit_candidate` holds the lock across load, validate and write today [C29].

`_rebuild_index_after_commit` runs **outside** the lock in the existing implementation [C30], and that
stays true: the index is derived, and a stale index after an interruption is recoverable from
`project.json`, and the implementation says so in the comment above that call [C30]. Nothing in this
protocol reads the index.

### 12.4 Fencing journal writes

Journal records carry `coordinator_run` and `ownership_generation` (§7.4) and are fenced by
publish-if-absent rather than by a lock. The rule is asymmetric on purpose:

- **A record from a superseded generation is not overwritten.** Publish-if-absent never clobbers, so a
  ghost writing `result.json` after a takeover either finds it identical (harmless) or gets `conflict`,
  which raises a `conflicting_publication` cause on the attempt (§7.2). It cannot corrupt anything.
- **A record from a superseded generation is not silently ignored either.** §6.3 accepts a record whose
  generation is the current one or an earlier one whose takeover is recorded; a record from a
  generation the reader cannot explain sets `indeterminate` (I5). So a record written by a ghost from a
  generation with no recorded takeover stops the projection rather than being read as history.
- **Registry writes are fenced by the registry lock and by the grant's own generation field.** O2's
  scan-and-insert re-reads every grant under the lock, so a grant written by a ghost is visible to the
  next scan and is not removed by anything except O17 or an operator.

There is no attempt to prevent a ghost from writing, and nothing stops a second coordinator being
started against one project [UNENFORCED U4]. Preventing either needs a fencing token the filesystem
enforces, which the envelope of §3.1 does not have. What the protocol guarantees is that a
ghost's write is either identical, or a recorded conflict, or an `indeterminate` projection — never a
silent divergence.

## 13. Recovery

### 13.1 The recovery scan

Recovery is not a distinct mode. At startup, and after any takeover, the coordinator runs one scan and
then enters the ready loop of §10.2 unchanged:

1. Read `project.json`. Establish ownership: either the recorded `coordinator_run` is this process's
   (a restart within the same run is impossible — a new process is a new run), or O17 with a proof
   (§12.2), or refuse.
2. Delete `runtime/`. It is derived and its staleness is not worth reasoning about (§7.1).
3. For every attempt directory not `RELEASED`, and every id in `execution.attempts`, compute `facts`
   with validation (§6.3). An `execution.attempts` entry with no attempt directory is itself a fact —
   O4's prefix after 2 — and is completed, not ignored.
4. For every attempt, match its durable-effect prefix against §8.2 and perform the stated completion.
   Completions are operations, so a crash here leaves another prefix.
5. Reconcile claims: every unreleased attempt must have a grant, and every grant must name an
   unreleased attempt. A grant naming an attempt that is `RELEASED` is removed (O16 step 2). An
   unreleased attempt with no grant and no `prepared` is disposed as a bare reservation; one with
   `prepared` or later and no grant is `indeterminate` — the record of what it was allowed to touch
   is gone, so nothing may be concluded about it.
6. Rebuild `runtime/projection.json` for the human, and report every attempt with an open cause, every
   refusal code encountered, and every `indeterminate` attempt, before doing any new work.

The scan performs no adapter calls except those §8.2's completions require, and it never dispatches.
Dispatch happens in the ready loop, after the report, which is what makes "recover, tell me, then
continue" the observable behaviour rather than "recover and immediately fill four slots".

### 13.2 Lost contact and staleness

Staleness is a trigger for investigation, never a conclusion (§12.2). An attempt is **stale** when

```
now - max(heartbeat.written_at if valid else -inf, launch.written_at) > config.stale_after
```

with `config.stale_after` defaulting to 900 seconds (§15). The `launch.written_at` fallback matters:
heartbeats are D2 and may be absent entirely — an adapter that cannot heartbeat, a `runtime/` that was
just deleted, a host without a writable temporary area — and a staleness rule that reads only
heartbeats declares every such attempt stale within one interval.

On staleness the coordinator does exactly this, in order:

1. Raise a `lost_contact` cause (O8). The attempt is now `HELD`, which is a report, not a verdict.
2. Ask the adapter to `observe` the handle in `launch.json`. Three answers, three dispositions:
   - **running** — the cause is resolved `rerun` with the observation as evidence, and the attempt
     continues. A slow task is not a failed one.
   - **finished** — proceed to `seal` (O6) if and only if the adapter attests the process tree exited
     and is not resumable (§17). Otherwise the cause stands; the coordinator cannot seal on a
     completion report alone (I2).
   - **unknown**, or no adapter contract for `observe` — the cause stands. The coordinator does not
     terminate and does not release.
3. If `uncertain_start` is set (O4's ambiguous prefixes), attempt **discovery**: look for the
   attempt's own store writes — any capture, any `result`, any log — which only the worker could have
   made. Their presence proves a start; their absence proves nothing, because the worker may not have
   reached its first write.
4. After `config.stale_grace` beyond the staleness bound, and only with an adapter that has a
   `terminate` contract, the coordinator may escalate to O15 (`stop`). Termination produces
   `stop-evidence`, and sealing still requires the adapter's attestation. Without such an adapter the
   attempt stays `HELD` until an operator resolves it.

At no point does staleness license removing a grant, dispatching a replacement for the same task, or
taking over ownership. The claim stays held; the project makes progress on other tasks if it can, and
reports if it cannot.

### 13.3 Reconciling partial outputs

A crashed or terminated attempt usually leaves files behind. The reconciliation compares three things:
the baseline from `prepared.json`, the declared write claims, and the current state of disk. Each
declared path gets a tag from §9.4's seven, and the tags drive the disposition:

| Observation | Disposition |
|---|---|
| every declared write `unchanged` or `still_absent` | nothing happened; the attempt is disposed and its task returns to `TODO` (O13, intent `TODO`) |
| some declared writes `created` or `modified`, and every check has an adequate qualifying capture | the work may have completed; §11.3's byte binding is re-tested against the current bytes and the attempt proceeds to O11 if it holds, or raises `stale_evidence` if it does not |
| some declared writes `created` or `modified`, and checks are missing or inadequate | a **partial write**: a `corrupt_record` cause is raised naming every changed path, and resolution is an operator's (`retry` after cleanup, `block`, or `withdraw`). The coordinator does not delete or revert files |
| any declared write `deleted`, `wrong_type`, or `unreadable` | the same partial-write path; these tags are never folded into "unchanged" |
| a path outside the declared write set changed | undetected: the reconciliation compares declared claims, and an undeclared write is invisible to it [UNENFORCED U1] |

**The coordinator never cleans up.** It does not delete a partially written file, does not `git
checkout` a modified path, and does not restore a baseline. Two reasons: the baseline is digests, not
content, so restoration is not possible from what the protocol stores; and a task's partial output may
be the most valuable thing the failed run produced. Reverting is an operator's decision made with the
tags in front of them.

## 14. Failure semantics and the dispatch gate

### 14.1 Causes

A **cause** is a recorded reason an attempt may not proceed. Fourteen classes, and every one of them is
raised by O8 and cleared only by O9:

| Class | Raised when | Ordinary resolutions |
|---|---|---|
| `check_failure` | a qualifying capture is `adequate: false` | `retry`, `block`, `skip`, `withdraw` |
| `indeterminate_check` | `exit` is `signalled`, `timeout` or `unknown` (§11.2) | `rerun`, `block` |
| `flaky_check` | the 16-rerun cap was reached (§11.4) | `block`, `withdraw`, `accept` with a human assessor |
| `plan_defect` | `spawn_failed`, or a claim unresolvable at run time | `block`, `withdraw` |
| `stale_evidence` | a qualifying capture's subject changed before acceptance (§11.3) | `rerun` |
| `definition_changed` | the re-derived `definition_hash` differs from the plan's (§11.3) | `withdraw` |
| `conflicting_publication` | publish-if-absent returned `conflict` (§7.2) | `withdraw`, `block` |
| `corrupt_record` | a record failed validation, or a subject is `unreadable`/`wrong_type` | `block`, `withdraw` |
| `store_error` | a normalized I/O error (§7.2) | `block` |
| `authorization_lost` | `authorization_in_force` became false mid-attempt | `withdraw` |
| `lost_contact` | staleness (§13.2) | `rerun`, `stop`, `block` |
| `human_review` | a judgement is required that no permitted assessor can make (§11.5) | `accept`, `block`, `skip`, `withdraw` |
| `operator_stop` | O15 | `stop` |
| `unsafe_release` | O16's precondition failed | `block` |

`open_causes` is the set of `cause_id`s in `holds/` with no matching file in `resolutions/`. It is a
fact (§6.1) and it appears in the preconditions of O4, O11, O13, O14 and O16 — which is the whole
mechanism. There is no separate "failure state" to be in.

### 14.2 The dispatch gate

The gate is a project-level condition: while it is closed, O4 dispatches nothing, for any task.

**It closes on classification and opens on resolution.** This is the correction the review asked for:
revision 4 wrote both "the gate clears when the failure is classified" and "the gate clears when the
operator resolves it", which are different protocols. Classification (O7) records *what happened*;
resolution (O9) records *what to do about it*. A gate that opens on classification opens as soon as
the coordinator has described the failure to itself, which is not a decision anyone made.

Precisely: the gate is closed when any attempt in the project has an open cause whose class is not
`operator_stop`. Stop-request holds are excluded because a stop is not a failure to diagnose — it is
an instruction being carried out, and O15's own sequence handles it; including it would mean a stop
request permanently closes a gate that no resolution is expected to open.

### 14.3 The consecutive-failure budget

Repeated failure must eventually stop the project rather than retrying forever, and the count must be
computable from the journal rather than held in memory.

The **failure run** is computed by scanning the project's `resolutions/` records across all attempts in
`decided_at` order and counting the trailing run of resolutions whose disposition is `retry` or
`rerun`. When that run reaches `config.max_consecutive_failures` (default 3, §15), the coordinator
raises `operator_stop` and stops dispatching.

The reset event is explicit and is exactly one thing: **a `commit-observed` record landing for any
task in the project.** Any real progress — one task accepted and confirmed — resets the run to zero.
Nothing else resets it: not a successful check inside a failing attempt, not the passage of time, not
a `block` resolution. `block` and `withdraw` do not extend the run either; they end an attempt rather
than retrying it, so they terminate the run without resetting it.

Computing the budget from ordered resolutions rather than an in-memory counter is what makes it
survive a restart, and it is why `resolution` records carry `decided_at` in the envelope's
`written_at` and a `decided_by` of their own (§7.4). A counter in the coordinator's memory resets on
every crash, which is the failure mode the budget exists to prevent.

## 15. Timing, volume, and observability

### 15.1 Settings

Every number in this document is a setting, stored in `config.json` at activation and immutable for the
generation (§7.6), so changing one is a recorded operator act. The defaults:

| Setting | Default | Why this value |
|---|---|---|
| `max_concurrent` | 4 | §3.2 measured maximum level width 3.87; above 4 the sampled workload cannot exercise it |
| `max_prepare` | 2 | preparation reads every input; more in flight is discarded work (§10.2) |
| `project_lock_timeout` | 5.0 s | the existing `DirectoryLock` default the commit path already uses [C24][C25] |
| `registry_lock_timeout` | 5.0 s | same lock implementation, same default; a scan-and-insert is milliseconds of work |
| `poll_interval` | 2.0 s | the ready loop's idle wait; below this the loop's own filesystem reads dominate |
| `heartbeat_interval` | 30 s | see §15.2's arithmetic |
| `stale_after` | 900 s | 30 missed heartbeats, or 15 minutes with no heartbeat at all |
| `stale_grace` | 900 s | additional time before O15 escalation is even considered (§13.2) |
| `check_timeout` | 1800 s | per check; produces `exit: timeout`, an `indeterminate_check` cause, not a failure |
| `max_reruns` | 16 | §11.4; also what makes §7.3's four-digit sequence unreachable |
| `max_consecutive_failures` | 3 | §14.3 |
| `log_cap` | 1 MiB per stream per capture | truncation is recorded in the capture (§11.2) |
| `tmp_reap` | 3600 s | age after which an orphaned `tmp/` file is removed (§7.2) |

The two lock timeouts are **5.0 seconds** because that is the default the shipped
`DirectoryLock.__init__` and `commit_candidate` already carry [C24][C25]. Revision 4 stated other
numbers; they were not the code's.

### 15.2 Heartbeat volume

At `heartbeat_interval` 30 s, four concurrent attempts produce 8 writes per minute, 480 per hour,
11,520 per day. Written as journal records those would be 11,520 files, retained (§7.7), fsynced, and
validated on every projection.

They are not journal records. `heartbeat` is the one **D2** record kind (§7.5): it lives under
`runtime/heartbeat/<attempt-id>.json`, is overwritten in place rather than appended, has no `sequence`
(§7.3), is never fsynced, and is deleted wholesale with `runtime/` on restart or takeover. The steady
state is therefore **four files**, rewritten 11,520 times a day, and a lost heartbeat costs nothing
because §13.2's staleness rule falls back to `launch.written_at`.

This is the direct consequence of I5's converse: heartbeats are the one thing whose absence *is*
treated as absence, and that is only sound because no decision is derived from them — staleness
triggers investigation, never a verdict.

### 15.3 What an operator sees

Three surfaces, and none of them is an authority:

- **The console**, during a run: each admission refusal with its code and task (§10.1), each dispatch,
  each result, each cause raised with its class, each resolution with its `decided_by`, and the
  recovery report of §13.1 step 6 before any new dispatch.
- **`runtime/projection.json`**, a machine-readable mirror of the current projection: per attempt, its
  facts (§6.1), its derived label (§6.2), its open causes, and its claims. Rebuilt each pass, deleted
  on restart, read by nothing that decides anything.
- **`evidence.md`**, the durable record, which gains one appended entry per `commit-observed`, keyed by
  `receipt_id` and deduped on it (§8.3). The append already happens under the project lock and as one
  atomic replacement, for the reason the implementation states: a commit validates the evidence files it
  is about to accept and must not see a half-written one [C16].

The console is where refusals must appear, because a refusal that only shows up in a JSON mirror is a
refusal the operator learns about by reading files after the fact. Every code in §4's table is
printed at the point of detection, with the task or attempt it applies to.

## 16. The worker contract

A worker is whatever the adapter starts. It may be a subagent, a subprocess, or the coordinator itself
executing inline. The contract is the same in all three cases, because a contract that is weaker for
inline execution is a contract the most-used path does not obey.

### 16.1 What a worker receives

Exactly two paths and one identifier: the attempt directory, the plan file, and the `attempt_id`. It
reads `plans/<task-id>.<plan-hash>.json` for its checks, claims, baseline and `cwd`, and
`prepared.json` for the baseline digests. It receives no project state, no task list, and no
authorization decision — those were made before it started, and a worker that could re-read them could
also disagree with them.

### 16.2 What a worker may write

- Its own outputs: every path in the plan's `writes[]`, and nothing else. `store:<attempt-id>` is
  implicitly granted (§9.2), so the worker writing its own records is legal.
- `captures/<check-id>/<NNNN>-<capture-id>.json`, one per check execution, with `assessor` null
  (§11.5).
- `logs/<check-id>/<NNNN>.out` and `.err`, truncated to `log_cap`.
- `result.json`, once, last, after fsyncing every declared output and its parent directory (O5).
- `captures/_conflict/<NNNN>-<capture-id>.json` when publish-if-absent returns `conflict` (§7.2),
  because a worker may not write a coordinator-owned `hold`.
- `runtime/heartbeat/<attempt-id>.json`, overwritten, if the adapter supports it.

It may not write `project.json`, `evidence.md`, `spec.md`, any other project file, any other attempt's
directory, the registry, or any coordinator-owned record kind (`reservation`, `prepared`, `launch`,
`sealed`, `classification`, `hold`, `resolution`, `stop-evidence`, `acceptance`, `commit-observed`,
`release`, `owner`). This is the existing canonical-state ownership rule [C5] restated at the level of
record kinds.

### 16.3 What a worker must do

1. Verify that `prepared.json` exists and that its `attempt_id` matches. If not, write nothing and
   exit non-zero: it was started for an attempt that does not exist.
2. Re-digest every read claim and compare to the baseline. A mismatch is not the worker's to resolve:
   it records the mismatch in `result.json` and does not run the checks. The coordinator raises
   `stale_evidence`.
3. Run each check in the plan's order, from the plan's `cwd`, capturing per §11.2. It records the exit
   variant it observed; it never converts `signalled` into a code, and never invents `unknown` for a
   status it did have.
4. Digest every declared write claim and every subject, before and after each check.
5. Publish `result.json` with `produced` (the §9.4 tags), `checks_run`, `baseline_matched`, and the
   ordered list of `capture_id`s it wrote.

### 16.4 What a worker must not do

- **It does not assess.** No `adequate`, no `assessor`, no `rationale`, no verdict of any kind (§11.5).
- **It does not decide the task's status.** `DONE`, `BLOCKED` and `SKIPPED` are canonical mutations and
  belong to the coordinator [C5][C6].
- **It does not retry a check.** Reruns are the coordinator's, sequenced (§11.4); a worker that retried
  internally would produce one capture describing several runs.
- **It does not leave descendants.** Every process it starts terminates before it publishes
  `result.json`. It cannot promise this on its own, and the protocol cannot inspect its
  descendants: only the adapter can attest it (§17), and an adapter that cannot is unusable for
  asynchronous dispatch (`R-DETACHED-CHILD`) [UNENFORCED U7].
- **It does not read another attempt's directory**, even to coordinate. Two workers never communicate.

### 16.5 Inline execution

Inline execution is this contract with the coordinator as the worker, in the same process, and it is
not a shortcut past anything: the attempt is reserved, granted, prepared, dispatched, captured,
sealed, classified, accepted, committed and released exactly as an asynchronous one is. Two things
differ, and both are restrictions rather than exemptions:

- The producer is the coordinator, so it may not assess its own `criteria` checks; those raise
  `human_review` (§11.5).
- Capacity is one, so the whole run is serial, and the `sealed` record for the `worker` scope is
  written when the inline call returns — which *is* an exit of the process tree, since there is no
  separate tree.

## 17. Host adapter contracts

An adapter implements four operations. Whether a host can implement each is a property of that host's
documented and tested behaviour, and this table is where that is written down.

| Operation | Meaning | What "supported" requires |
|---|---|---|
| `start(plan, attempt_dir) -> handle` | begin execution, return an identifier | the call either starts the work or fails; a return value that means "maybe" is `ambiguous` (§8.2) |
| `observe(handle) -> running \| finished \| unknown` | report liveness without changing anything | it must be able to answer for a handle from a *previous* coordinator process |
| `seal(handle) -> attestation` | attest the process tree exited and cannot be resumed | both clauses: exited **and** not resumable. This is the I2 barrier |
| `terminate(handle)` | request termination of the whole tree | it must cover descendants, not only the named process |

### 17.1 The matrix

**Every row below is unverified.** No adapter has been written, and nothing in this repository tests
any of these. The column is a *requirement placed on Stage 7* (§20), not an observation.

| Host | Version | `start` | `observe` | `seal` | `terminate` | Consequence |
|---|---|---|---|---|---|---|
| Claude Code | 2.x | plausible via the Agent tool | plausible via agent listing | **not available** | plausible via task stop | `R-DETACHED-CHILD`: inline only |
| Codex | current | not investigated | not investigated | not investigated | not investigated | `R-NO-ADAPTER`: inline only |
| Kimi Code | current | not investigated | not investigated | not investigated | not investigated | `R-NO-ADAPTER`: inline only |
| Local subprocess | POSIX | `posix_spawn` in a new process group | `waitpid` / `kill(pid, 0)` | plausible: process group reaped, no session leader left | `killpg` | the candidate for Stage 7 |

The Claude Code row is the load-bearing one, and it is not an argument from documentation. The
evidence is the running host's own tool surface: a completed agent can be resumed by sending it a
message, which resumes it from its transcript. A completion report from such a host therefore means
"this agent is not currently producing output", not "this agent's execution has ended and cannot
restart". `seal` requires both clauses, so it is unavailable, and by §3.1's Processes row every
asynchronous attempt on that host refuses with `R-DETACHED-CHILD`.

**The consequence is worth stating plainly: on the host this design is written for, v1 runs inline at
capacity one.** Parallel dispatch is available only where an adapter can seal, which today means a
local subprocess adapter that nobody has written. A design document that obscured this by describing
subagent dispatch as the primary path would be describing something it cannot deliver.

### 17.2 Ambiguous start does not license inline replay

If `start` returns ambiguously (§8.2, O4 after 4), the coordinator does **not** fall back to running
the task inline. The capability was issued; a worker may be running; running the same checks inline
would put two writers on one claim set, which is exactly what I1 exists to prevent. The attempt is
`UNCERTAIN`, §13.2 investigates, and the task does not run again until the attempt is released or an
operator resolves it. `R-DETACHED-CHILD` and `R-NO-ADAPTER` refuse *before* dispatch, which is why
they are safe and this is not.

### 17.3 What an adapter may not do

An adapter may not write journal records, may not touch canonical state, may not choose what to run,
and may not report `finished` as an attestation. `observe` returning `finished` and `seal` returning an
attestation are different claims with different consequences (§13.2), and an adapter that conflates
them silently defeats I2. Nothing in the protocol can check the attestation: it has no view of the
process tree, so a `seal` that is wrong is believed [UNENFORCED U7].

## 18. Authoring plans that can actually run in parallel

The protocol refuses rather than guesses, so whether a project gets any parallelism at all is decided
by how its tasks are written. Five rules, each of which maps to a refusal code.

1. **Declare every output with its root**, and mark it `required` when success depends on it
   [C5][C6][C7]. An undeclared output is invisible to the conflict relation (§9.3) and to
   reconciliation (§13.3). An output that is `required: true` and covered by no check refuses
   admission (`R-CHECK-UNCOVERED`).
2. **Write `verification` as commands, one per line**, with no `&&`, `||`, pipelines, redirection or
   unquoted `;` (§9.1). A composed command is not enumerable, so the task runs inline
   (`R-UNENUMERABLE`). Two lines run in parallel-eligible form; one line joined by `&&` does not.
3. **Name files, not directories**, as subjects and outputs. A directory subject refuses
   (`R-DIRECTORY-SUBJECT`) because §9.2 has no digest for one and §22 defers the semantics.
4. **Keep effects confined**, with `effect.kind` `none` or `local_write` and `effect.confined_to`
   covering every write. Anything else refuses (`R-EFFECT-NOT-CONFINED`); a task with an external
   effect is a task the protocol will not dispatch asynchronously at all [C11].
5. **Split by resource, not by phase.** Two tasks that write the same file conflict (§9.3) however
   independent their descriptions are. Two tasks that read the same file do not. The largest
   practical win is from separating write sets, which is a property of the plan, not of the scheduler.

And the honest counterweight: §3.2 measured a median parallel width of 1.42 across 26 real graphs.
Following every rule above, most projects will still run mostly serially, because most of their tasks
depend on each other. The rules buy correctness at the boundary and a modest win where width exists;
they do not create width.

## 19. Cooperative rules the protocol does not enforce

Each rule below is stated at the site where it matters, and marked there. This section is the index,
not the statement: a reader who finds only this list has found a promise without a location.

| Id | Rule | Stated at | Why it is unenforced |
|---|---|---|---|
| U1 | Do not write paths you did not declare | §9.3, §13.3 | the protocol compares declared claims; the filesystem is not restricted |
| U2 | Do not retarget a symlink under a declared path after preparation | §9.2 | canonicalization happens once, at preparation |
| U3 | Do not edit a task's definition while an attempt is in flight | §11.3 | detected at acceptance and refused there, not prevented |
| U4 | Do not run two coordinators against one project | §12.4 | fenced, and a ghost's writes are conflicts or `indeterminate` — not prevented |
| U5 | Do not hand-edit the execution store | §7.2 | publish-if-absent stops clobbering, not authorship |
| U6 | Do not share a store across volumes | §7.6 | probed at activation; a later mount change is undetected |
| U7 | Do not rely on a worker's process tree exiting without an adapter attestation | §16.4, §17.3 | the protocol has no way to inspect descendants |

Each site named above carries an `[UNENFORCED U<n>]` marker in its own prose, and §21.1's check relates
every row to a marker inside every section it names — so moving a rule's statement without moving its
marker fails, which counting markers would not catch.

## 20. Implementation stages

Each stage is independently useful, and each is verifiable before the next begins.

| Stage | Deliverable | Behaviour change |
|---|---|---|
| 1 | The record schemas, canonical serialization, digests and `publish_if_absent`, with unit tests including crash injection between durable effects | none |
| 2 | The measurement harness of §3.2, committed with its graph fixtures, so the width figures are reproducible | none |
| 3 | The fact projection (§6.1), record validation (§6.3) and the derived label (§6.2), plus the stateful reference model of §21.2 | none |
| 4 | The operation table (§8.1) as executable operations with their crash-prefix completions (§8.2), against a fake store | none |
| 5 | The registry (§10.4), the plan resolver (§9.1, §11.1), the conflict relation (§9.3) and the admission gate (§10.1), all refusing with codes | none |
| 6 | `enable-execution`, the v4 schema field, and the reader-only refusal (§7.6) | **yes** — a v4 project is unreadable by an installation without this stage, and activation is irreversible for the generation |
| 7 | The local-subprocess adapter, and inline execution through the same contract (§16.5) | **yes** — tasks begin executing under the protocol |
| 8 | Asynchronous dispatch at capacity > 1, gated on Stage 2's numbers and on an adapter that can `seal` | **yes** — concurrency |

Two stages change behaviour before the concurrency does, and revision 4 claimed only one did. Stage 6
is the one that matters: activation writes a schema field that older installations must refuse, so it
is behaviour-changing for every host that has not been upgraded, and the migration story belongs to it
rather than to Stage 7.

Stage 8 is explicitly conditional. If Stage 2's numbers on real graphs do not show a win, the correct
outcome is to stop at Stage 7 with a protocol that runs inline, correctly, and refuses visibly — which
is a better result than concurrency nobody measured.

## 21. What is verified, and what is not

### 21.1 The documentation checks

`tests/plugins/research/test_parallel_execution_doc.py` checks relationships between this document,
the shipped code, and the review record. It checks that:

- every section this document promises exists, and every retired section name is gone;
- every constant cited from `workspace_lib.py` matches the source, parsed with `ast` rather than
  imported, so the checker carries no coverage burden from the package under `--cov`;
- the two lock timeouts of §15.1 equal the defaults the shipped `DirectoryLock.__init__` and
  `commit_candidate` actually carry — read out of the source, not out of §23's prose — and every other
  setting §15.1 defines is named somewhere outside that table;
- every canonical transition cell in §8.1 parses **completely** under one grammar and names a
  transition present in `TASK_TRANSITIONS`, with no unparsed remainder — a cell of unrelated prose
  fails rather than yielding no findings — and no precondition in §8.1 is stated over a label of §6.2,
  which is the structural half of "labels gate nothing";
- every name §6.2 matches on is a fact, or a declared fact value, of §6.1, and every fact §6.1 declares
  is read somewhere outside it;
- every refusal code has exactly one row in §4, is named outside §4, and every code used anywhere in
  the document has a row;
- every `[Cn]` use resolves to a row in §23, and every row in §23 is used;
- every internal Markdown link resolves, **including its fragment**, to a heading in the target file;
- operation ids in §8.1 and rule ids in §19 are unique and contiguous from 1, and every id cited
  outside its own table resolves to a row in it;
- every row of §19 has an `[UNENFORCED U<n>]` marker inside **each** section it names, and every such
  marker in the document belongs to a row of §19 and stands in a section that row names;
- every review file in `docs/` is present, the revision each was assessed against and the revision that
  answered it are both recorded, those pairs form an unbroken chain, and the newest review is answered
  by this revision — so shipping a revision without dispositioning the review it responds to fails.

**What these checks cannot do, with a worked example.** §15.1 cites [C24][C25] for the 5.0-second lock
timeouts. The checker verifies that C24 and C25 are rows in §23, that those rows name
`workspace_lib.py` lines, and that the constants at those lines are what §23 says. It does **not**
verify that 5.0 seconds is the right timeout for a registry lock, that the registry lock should use the
same implementation, or even that the sentence citing them is about locks at all. A citation that
resolves proves the reference exists; it says nothing about the claim standing next to it. The same
holds for every other check here: they are consistency checks on a document, and a consistent document
can describe an incorrect protocol.

### 21.2 The reference model

`tests/plugins/research/test_parallel_execution_model.py` is a small stateful model, not a table of
label combinations. It has two coordinators, two tasks, one shared write path, one checker, and one
launch capability. Canonical state and external execution are modelled **separately**, so "the worker
is running" is not derived from "the record says running". Recovery is modelled as a function from a
store to a sequence of operations, whose durable effects are then applied — so recovery is exercised
rather than asserted. Crashes are injected between durable effects, at every prefix of every
operation's sequence.

The properties it asserts are the five invariants, stated externally:

- **I1** no two executors ever hold conflicting claims, checked against the modelled registry, not
  against a label;
- **I2** no claim is released while any modelled process can still write, including a process whose
  coordinator is dead and whose capability is unconsumed;
- **I3** an acceptance's `accepted_snapshot` equals the modelled bytes at commit time, and its
  `definition_hash` equals the modelled definition;
- **I4** a task is `DONE` only if its `evidence` holds the receipt, checked by building the real
  candidate and dry-running it through the real validator;
- **I5** no projection reads a malformed or unexplainable record as absence.

Each invariant is also shown breakable with the guards bypassed, because an invariant no scenario can
violate is not being checked.

Ten of review 04's eighteen findings are traces of this state machine, and each has a named regression
case: R4-01, R4-02, R4-03, R4-04, R4-06, R4-07, R4-08, R4-10, R4-12 and R4-14. The other eight are not
state-machine traces and are deliberately not modelled here — R4-05 and R4-11 are answered by §6 and
§11.1, R4-09 and R4-13 by refusals in §4, R4-15 by this replacement itself, R4-16 by §17.1, and R4-17
and R4-18 by the documentation checker and §21.3.

The suite is deliberately far smaller than revision 4's: 39 tests and 56 subtests here against 70 tests
and 1,208 subtests there. That suite asserted label names, and its `apply_outcome` returned a constant
regardless of the crash prefix it was given, so most of those subtests could not fail.
**Passing this model does not establish that the protocol is correct.** It is a design-stage model of a
design; there is no executor, no filesystem, and no adapter behind it, and the real validator it calls
checks a candidate's schema and transitions rather than a receipt's meaning, so the receipt half of I4
is the model's own assertion.

### 21.3 The benchmark

Not written. §3.2's figures come from a one-off script that is not committed, on 26 graphs from one
workspace. Stage 2 commits the harness and the fixtures. When it does, the benchmark must report
repeated runs rather than one, and must include a tiny-task case — a graph whose tasks each take under
a second — because that is where per-attempt store overhead can exceed the concurrency win, and no
current number addresses it.

Nothing in this document is verified against a running implementation, because there is none.

## 22. Deferred and out of scope

Each item below is refused visibly today, with the code named, and is a candidate for a later revision.
Listing it here is the alternative to promising it vaguely. Why each was deferred rather than built is
in the companion's
[rejected, deferred and revisited record](../../../../../docs/parallel-execution-decisions.md#rejected-deferred-and-revisited).

| Deferred | Refused today by | What it would take |
|---|---|---|
| Takeover of a live or unprovable predecessor | `R-LIVE-PREDECESSOR` | a fencing token the filesystem or a lease service enforces, so a ghost's write can be rejected rather than detected |
| Detached, background or resumable execution | `R-DETACHED-CHILD` | an adapter that can attest a process tree has exited and cannot restart, on a host that can promise it |
| Unenumerable inputs and composed commands | `R-UNENUMERABLE` | either a resolved-claim declaration authored by hand, or a sandbox that observes actual reads and writes |
| Directory outputs and subjects | `R-DIRECTORY-SUBJECT` | a defined digest for a directory as a unit, including ordering, permissions and what counts as a change |
| External references and effects | `R-EXTERNAL-REFERENCE`, `R-EFFECT-NOT-CONFINED` | an authorization model for effects a filesystem snapshot cannot describe [C11] |
| Cross-workspace coordination | the registry is per workspace (§10.4) | a shared registry with its own availability and trust story |
| A second independent assessor, so an inline producer's `criteria` checks are not operator-blocked | `human_review` holds (§11.5) | a second process with its own identity, which is a second executor and therefore its own I1 problem |
| Alternate transports and stores (a database, a queue, a remote store) | the envelope of §3.1 | a durability and ordering argument for the replacement, not a port |
| Legacy-writer coexistence beyond an operator attestation | `R-LEGACY-WRITER` | a version negotiation older installations participate in, which they cannot, being older |
| Scheduling heuristics beyond plan order | §10.2 states plan order as the only tie-break | a measured win over plan order, which §3.2's widths suggest is unmeasurable at this scale |

## 23. Citations

Every row is re-read by the documentation test: the file is opened, the line range is sliced, and the
needle must appear inside it. A row whose needle has moved fails the test rather than aging quietly.
Each row is also required to be *used* somewhere in this document, and every `[Cn]` use is required to
resolve to a row — so a citation cannot be added and forgotten, or cited and never defined.

Read §21.1 before treating a row as verification of anything: it establishes that the reference exists,
not that the sentence citing it is true.

| Id | Source | Lines | Needle |
|---|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1345-1352 | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2765-2790 | `def _dependency_levels(` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2788-2815 | `def build_task_graph(` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2685-2692 | `def levels(` |
| C5 | `plugins/research/skills/project/SKILL.md` | 180-184 | `One coordinator owns writes to` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md` | 9-14 | `One coordinator is the sole writer` |
| C7 | `plugins/research/skills/project/SKILL.md` | 382-386 | `Use a dependency graph only when independent work can run in parallel` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py` | 319-352 | `class DirectoryLock` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py` | 296-312 | `def atomic_write_text(` |
| C10 | `plugins/research/skills/project/scripts/workspace_lib.py` | 26-30 | `EFFECT_KINDS = {` |
| C11 | `plugins/research/skills/project/scripts/workspace_lib.py` | 33-37 | `REFERENCE_ROOTS = {` |
| C12 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2244-2252 | `unsupported schema_version` |
| C13 | `plugins/research/skills/project/scripts/workspace_lib.py` | 455-472 | `def _validate_evidence_reference(` |
| C14 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2540-2552 | `shell=False,` |
| C15 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2553-2558 | `command timed out after` |
| C16 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2584-2600 | `must not see a half-written one` |
| C17 | `plugins/research/skills/project/scripts/workspace_lib.py` | 26-30 | `AUTHORIZATION_STATUSES = {` |
| C18 | `plugins/research/skills/project/scripts/workspace_lib.py` | 538-544 | `non-required authorization must use status` |
| C19 | `plugins/research/skills/project/scripts/workspace_lib.py` | 550-556 | `task requires explicit authorization` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py` | 59-65 | `TASK_TRANSITIONS = {` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3113-3117 | `IMMUTABLE_PROJECT_FIELDS = (` |
| C22 | `plugins/research/skills/project/scripts/workspace_lib.py` | 24-30 | `TASK_STATUSES = {` |
| C23 | `plugins/research/skills/project/scripts/workspace_lib.py` | 548-554 | `source and authorized_at must be null unless status is explicit` |
| C24 | `plugins/research/skills/project/scripts/workspace_lib.py` | 320-326 | `def __init__(self, path: Path, timeout: float = 5.0) -> None:` |
| C25 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3193-3200 | `lock_timeout: float = 5.0,` |
| C26 | `plugins/research/skills/project/scripts/workspace_lib.py` | 417-435 | `required must be a boolean` |
| C27 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1338-1346 | `BLOCKED task requires block_reason` |
| C28 | `plugins/research/skills/project/scripts/workspace_lib.py` | 82-96 | `TASK_FIELDS = {` |
| C29 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3200-3226 | `with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):` |
| C30 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3226-3232 | `The commit already landed` |
| C31 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1286-1292 | `"success_criteria", "verification"` |

Eight rows are new in revision 5, and each exists because revision 4 asserted the constant without one:
C22 (`TASK_STATUSES`), C23 (the withdrawal rule that `source` and `authorized_at` must both go null),
C26 (`required` is a boolean on every output object), C27 (`block_reason` iff `BLOCKED`), C28
(`TASK_FIELDS`, which has no `notes` key), C29 (the lock the commit path holds across load, validate
and write), C30 (the index rebuild outside it), and C31 (`verification` as a required non-empty
string). C24 and C25 replace revision 4's stated lock timeouts with the code's actual 5.0-second
defaults.
