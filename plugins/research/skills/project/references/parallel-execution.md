# Parallel task execution

> **Status: implemented and verified for the 0.8.1 release candidate.**
> Fresh projects initialize as schema v4, and `research-project run-auto` resolves immutable plans,
> launches Claude Code or Codex CLI workers in isolated Git worktrees, verifies their outputs, and
> serially integrates accepted commits. Unsupported shapes return precise sequential-fallback reasons.
> The implementation sequence and remaining pilot gate are in
> [`docs/parallel-execution-implementation-plan.md`](../../../../../docs/parallel-execution-implementation-plan.md)
> (§20).
>
> **Revision 6, 2026-09-11.** The objective is **lower completion time and lower coordinator context
> consumption at preserved quality and controlled total cost**, for one `research:project` plan whose
> independent tasks are dispatched by one coordinator to subagents, published through a durable
> journal, and integrated into canonical state by that same coordinator, which remains the sole writer
> of it. Revision 5 narrowed the envelope, and that narrowing stands: five invariants each with a named
> authority and synchronization point (§1); an explicit list of the task shapes and hosts v1 supports
> (§3); and a visible refusal at admission for everything else (§4).
>
> Revision 6 does not widen it. It repairs the **data contracts that could not represent the work the
> envelope admits** — one mutation path per mutation intent rather than one path serving two (§7.4); a
> digest whose input is well defined, and an immutable record that no longer has fields written into it
> after the fact (§11.2, §11.5); a durable stop request the coordinator cannot forget across a restart
> (§10.1); scope identity that distinguishes a check's second invocation from its first (§6.1); and
> explicit operations for taking and giving up ownership, without which the closure gate of §7.6 was
> unreachable (§8.4). It also **stops promising the parts that are not settled**: an unsupported task
> shape now refuses and quiesces rather than falling back to an inline path whose semantics were never
> specified (§4), and `effect.confined_to` is withdrawn because the shipped validator rejects it [C32].
>
> What this document does **not** decide is now said as plainly as what it does. Which host v1 first
> executes on, whether a separate agent can be given a task and observed at all, and whether the win is
> real are **phase-gated questions** (§20), settled by building a phase and reviewing it, not by another
> pass over this prose. Read this document as the protocol a phase implements, not as an
> implementation-ready specification for the whole executor.
>
> Two structural properties carry over from revision 5. Labels gate nothing: §6 defines *facts*, and the
> label is derived from them for display only, so recording a real asynchronous event never requires a
> transition the graph forbids. And every operation (§8) is a precondition over facts plus an ordered
> sequence of durable effects with a stated replay rule, so a crash has a prefix and the prefix has a
> completion.
>
> This document is the normative reference: current rules, schemas, operations and failure behaviour.
> It is self-contained, because `docs/` is not part of the installed plugin subtree — nothing an
> executing coordinator needs is outside this file. The history, the disposition of all five expert
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
  edit canonical state [C5][C6]. The default workflow is sequential; worker execution is selected
  only when requested/authorized and useful [C7]. Parallel admission below still requires declared,
  non-conflicting claims.
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
| Task shape | `effect.kind ∈ {none, local_write}`; inputs, outputs and each check's read and write sets exhaustively enumerable as local file paths; every output path a regular file; no `criteria` check whose only available executor is the coordinator itself | §9, `R-UNENUMERABLE`, `R-DIRECTORY-SUBJECT`, `R-EFFECT-NOT-CONFINED`, `R-SELF-ASSESSMENT` |
| References | `target`, `workspace` and `workspace_root` outputs only. `external` references are URL-like, not paths, and no rule here maps them onto claims | §9.2, `R-EXTERNAL-REFERENCE` |
| Ownership | a coordinator takes over only from a predecessor proven dead; otherwise the predecessor's grants stay held and the project stays stopped until an operator resolves it | §12.2, `R-LIVE-PREDECESSOR` |
| Capacity | at most four concurrent attempts, counting reserved and ambiguous launches, and counting the coordinator's own inline work | §10.3 |
| Hosts | one adapter, conforming to §17's matrix, with a tested `start`/`observe`/`seal`/`terminate` contract. Every other host runs inline at capacity one | §17, `R-NO-ADAPTER` |
| Runner | a **long-lived coordinator process** that outlives every attempt it dispatches, so that ownership, the ready loop and recovery have somewhere to live (§12.1) | §12.1, `R-NO-RUNNER` |

Inline execution at capacity one is inside the envelope, but it is **not** a fallback for a task shape
the envelope excludes, and revisions 1 to 5 conflated those two things. It is the same scheduler at
capacity one (§10.2), not a second code path, and it is not exempt from the registry: an inline executor
takes a grant like any other, so a legacy writer in the same workspace is a conflict the registry can
see. That is what makes it a legal *host* answer — `R-NO-ADAPTER` and `R-DETACHED-CHILD` refuse a way of
starting a worker, not the work, so the same operations, claims and records still apply.

A refused *shape* is different: there is no claim set to grant, no digest to compare, or no receipt the
protocol can hold, so there is nothing for an inline attempt to be an attempt *of*. Those refusals
(`R-UNENUMERABLE`, `R-DIRECTORY-SUBJECT`, `R-EXTERNAL-REFERENCE`, `R-EFFECT-NOT-CONFINED`,
`R-SELF-ASSESSMENT`) hand the task back to the existing v3 sequential path — outside the protocol,
outside the registry, with no attempt and no records — after the coordinator **quiesces**: it settles
every in-flight attempt and dispatches nothing new until that task is done. Quiescing is the price of
having one writer; running an unprotocolled task beside protected attempts would put a writer the
registry cannot see next to claims it is supposed to protect.

### 3.2 The measurement that sets the ceiling

Dependency levels were **recomputed on 2026-09-11** over every project in the reference workspace —
34 projects, 592 tasks, 331 dependency levels, all of the projects having five or more tasks — and
achievable speedup modelled with effect-bearing tasks serialized on the coordinator. Revision 5's table
was computed when the same workspace held 31 projects and is superseded by the rows below; the figures
moved by less than the sample grew.

| Quantity | Value |
|---|---|
| Projects, tasks, dependency levels | 34 / 592 / 331 |
| Mean average dependency-level width | 1.86 |
| Median average dependency-level width | 1.75 |
| Dependency levels holding exactly one task | 202 of 331 (61%) |
| Maximum level width, per project | mean 3.91, median 3, range 1 to 13 |
| Projects whose widest level exceeds 4 | 8 of 34 |
| Fully linear projects (width 1.0 throughout) | 2 of 34 |
| Modelled speedup, unlimited workers | 1.45x mean, 1.37x median, 2.21x best, 1.00x worst |
| Projects at or below 1.2x | 13 of 34 |
| Concurrency cap 2 / 4 / 8 / unlimited | 1.24x / 1.41x / 1.45x / 1.45x mean |

The model is **generous**: it charges a batch the cost of one task rather than its slowest member, and
charges nothing for spawning, prompt authoring, result integration, extra commits, or contention.
Loaded honestly, some of these projects would run *slower* in parallel. Dependency-level width is a
*structural* property of a plan; achieved scheduling depends on task durations, resource conflicts,
verification and integration cost, and host capacity, none of which appears above. The figures come
from a one-off script run against one private workspace on the date named above; that workspace is not
in this repository, so the harness and its graph fixtures are committed by Phase 6 (§21.3) and until
then the figures are reproducible in principle and **not yet reproducible in this repository**.

Four consequences shape everything below, and the third is a correction.

- **Wall-clock speedup is not the justification.** A design sold on 1.45x, measured optimistically
  against overheads it does not count, would not survive contact with a real project. Context economy
  is the plausible larger win on a host with bounded worker context (§17), and it is a hypothesis with
  a benchmark attached (§21.3), not an established benefit.
- **Most of a plan is a line.** 202 of 331 levels hold one task, and the median project's average width
  is 1.75. Width is something a minority of levels have, so every rule below is written for the case
  where there is nothing to parallelize.
- **This measurement does not choose a concurrency bound.** Revisions 1 to 5 argued that a cap of 4 was
  right because the *mean of per-project maxima* is 3.91. That inference is withdrawn: a mean of maxima
  is not a bound on demand — 8 of these 34 projects have a level wider than 4 and the widest holds 13 —
  and being within 0.04x of unlimited at a cap of 4 is a property of the generous model, not evidence
  about any executor. What the sample does support is narrower: at a cap of 2 the model still shows
  1.24x, so the smallest bound above sequential is where a pilot can look for a win. §10.3 and §15.1
  therefore state `max_concurrent` as a **configured bound with a default of 2 and a ceiling of 4**, and
  claim nothing about optimality; §21.3 is where a different number would have to be earned.
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
| `R-NO-RUNNER` | the host cannot keep a coordinator process alive for the duration of an attempt (§12.1) | activation | the whole protocol is unavailable; the project stays on today's sequential path and no execution store is created |
| `R-NO-ADAPTER` | the host has no adapter conforming to §17 | activation | capacity one, inline under the protocol; parallel dispatch unavailable |
| `R-EFFECT-NOT-CONFINED` | `effect.kind ∉ {none, local_write}` | admission | **not admitted**: the coordinator quiesces and the task runs on the v3 sequential path, which is where a `destructive` or `external` effect and its authorization already live |
| `R-UNENUMERABLE` | the execution plan (§9.1) cannot list the task's reads, its writes, or a check's writes | admission | **not admitted**: quiesce, then the v3 sequential path. There is no coarse-claim fallback; a claim the plan cannot enumerate is not a claim |
| `R-DIRECTORY-SUBJECT` | a check subject or an output is a directory | admission | **not admitted**: quiesce, then the v3 sequential path; §9.2 has no digest for a directory and §22 defers one |
| `R-EXTERNAL-REFERENCE` | an output's root is `external` | admission | **not admitted**: quiesce, then the v3 sequential path; no rule here maps an external reference onto a claim or a snapshot |
| `R-SELF-ASSESSMENT` | the plan has a `criteria` check and the only executor available for it is the coordinator itself (§11.5) | admission | **not admitted**: quiesce, then the v3 sequential path, where the coordinator assessing its own work is the status quo rather than a new protocol claim |
| `R-CHECK-UNCOVERED` | a `required: true` output has no check whose subject set contains it | admission | the task is not admitted at all, in parallel or inline: the plan cannot show the output was produced |
| `R-DETACHED-CHILD` | an adapter cannot promise that the attempt's process tree has exited when it reports completion | activation, per operation in §17's matrix | that adapter is not usable for asynchronous dispatch |
| `R-LIVE-PREDECESSOR` | ownership is stale but the predecessor is not proven dead (§12.2) | activation | no takeover; existing grants stay held; the coordinator reports the operator action needed |
| `R-INDETERMINATE` | any record of an active attempt fails validation, or carries a generation the current run cannot explain | admission, and inside every projection | the attempt is `INDETERMINATE`; dispatch is blocked and no claim is released until an operator resolves it |
| `R-CAPACITY` | granting would exceed four concurrent attempts including reservations | admission | the task waits; this is the one refusal that resolves itself |

Every "not admitted" row above sends the task to the **same place**: the sequential executor
`research:project` uses today, with the coordinator quiesced first (§3.1). That is a deliberate
narrowing of revision 5, which promised these tasks would "run inline" under the protocol. They cannot:
`R-UNENUMERABLE` has no claim set, `R-DIRECTORY-SUBJECT` has no digest, `R-EXTERNAL-REFERENCE` has no
snapshot, `R-EFFECT-NOT-CONFINED` has an authorization the protocol does not carry, and
`R-SELF-ASSESSMENT` has no second assessor — so an inline attempt for any of them would have been an
attempt whose records could not be validated by §6.3 or accepted by §11.3.

Three refusals deserve their reason stated, because they cost real capability.

- `R-UNENUMERABLE` is the load-bearing narrowing of this revision. Revision 4 derived read claims from
  dependency outputs and check subjects only, which cannot express an analysis task reading a source
  file that no dependency produced, a check that writes a cache, or a repository operation that
  rewrites a working tree. Rather than compare claims that are known to be incomplete and call the
  comparison prevention, v1 admits to parallel execution only the task shapes whose reads and writes
  the plan can list, and sends everything else through the coordinator. §9.1 says what a plan must
  list; §19 records that undeclared writes remain outside enforcement.
- `R-CHECK-UNCOVERED` is the one row that refuses the task outright rather than routing it anywhere,
  because the defect is in the plan, not in the executor. A required output nothing checks is a task
  whose success cannot be established by any executor, sequential or otherwise, so handing it to the
  sequential path would be handing over a task that cannot succeed.
- `R-SELF-ASSESSMENT` replaces a worse rule. Revision 5 said an inline producer's `criteria` check
  raises `human_review` and waits for an operator (§11.5), which made *every* judged task without an
  independent assessor block on a human and called that a safety property. It is not:
  the coordinator already assesses its own `criteria` checks on the sequential path today, and doing so
  is not a regression the protocol needs to fix. So the protocol declines the task instead of changing
  how the task is judged. `human_review` remains what §11.5 uses when a *dispatched* attempt's
  `criteria` check has no assessor, which is a case that only exists once Phase 2 (§20) shows a
  dispatched worker is possible at all.

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
| canonical | `revision`, `coordinator_run`, `ownership_generation` | integer, string, integer | `revision`, `execution.coordinator_run`, `execution.ownership_generation` |
| canonical | `authorization_in_force` | bool: `required` is false, or `status` is `explicit` | the task's `authorization` [C18] [C19] |
| canonical | `receipt_present` | bool: the receipt tuple of §11.3 is in the task's `evidence` | `project.json` |
| plan | `plan_hash`, `definition_hash` | digests | `plans/<task-id>.<plan-hash>.json`, §9.1 |
| journal | `reserved`, `prepared`, `launched` | bool | the matching record, valid |
| journal | `result` | null, or `success` / `failure` / `refused` | `result.json`'s `outcome` |
| journal | `declared_scopes` | set of scope ids | one per `scope` record under `scopes/`, and nowhere else |
| journal | `sealed_scopes` | set of scope ids | `sealed/` |
| journal | `open_scopes` | `declared_scopes - sealed_scopes` | derived |
| journal | `open_causes` | set of cause ids with a `hold` and no matching `resolution` | `holds/`, `resolutions/` |
| journal | `stop_evidence` | null, or one of §13's kinds | `stop-evidence.json` |
| journal | `classified` | null, or a class from §14 | `classification.json` |
| journal | `accepted`, `commit_observed`, `released` | bool | for the first two, per mutation: `mutations/<receipt-id>/intent.json` and its `ack.json`. An attempt is `accepted` when some mutation with a terminal intent has an intent record, and `commit_observed` when that mutation has its ack |
| project | `stop_requested` | bool | an unmatched `stop-request` under `control/`, §10.1 |
| journal | `uncertain_start` | bool: the adapter answered `ambiguous`, or the caller died inside `start` | `launch.json`'s `start_outcome`, or its absence beside a consumed capability |
| registry | `grant_present` | bool | `grants/<attempt-id>.json` |
| registry | `launch_capability` | `none`, `issued`, `consumed`, or `revoked` | the grant's `capability` field, §12.2 |
| validity | `indeterminate` | bool, with the offending record named | §6.3 |

**Execution scopes** are the unit I2 is stated over. An attempt declares one scope per thing that may
write on its behalf, and it declares it by **publishing a `scope` record before that thing may write**
(§7.4). There are exactly two shapes of scope id:

- `worker` — the dispatched process tree, or the coordinator's own inline execution. Published by O3
  as `scopes/worker.json`, before any dispatch.
- `check:<check-id>#<NNNN>` — one per *execution* of a check, where `NNNN` is the same run sequence the
  capture carries (§7.3). Published by O10 as its first durable effect, before the check runs.

A scope is **sealed** when a `sealed` record exists for it, which the adapter may write only when the
scope's whole process tree has exited and the host cannot resume it.

Revision 5 put a fixed `declared_scopes` list in the `prepared` record and gave check scopes the id
`check:<check-id>`, which broke on the two things the rest of the design does. Reruns: `max_reruns` is
16 (§11.4), so one check can execute up to 16 times, and 16 executions sharing one scope id cannot each
be sealed — the second seal is a `conflict` against the first, and a coordinator could not tell a
sealed rerun from an open one. Enumeration: which checks run, and how many times, is not known when
`prepared` is written, because a rerun is triggered by a cause raised later. Putting the declaration in
the record that *authorises the write* fixes both: `declared_scopes` is the set of `scope` records
present, it grows as executions happen, and `open_scopes` shrinks only when a seal names the exact
execution. This is also what makes "the coordinator's verification is not covered by the attempt's
grant" inexpressible — a check with no `scope` record has not been authorised to write anything.

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
present with the right JSON type; `project_id` matches, and for an attempt-level kind `attempt_id` and
`task_id` match the attempt being projected (a project-level kind carries neither, §7.4); `body_digest`
recomputes; and `ownership_generation` is one this run can explain.

"Can explain" has two clauses, not one. The generation is the current one — or it is an earlier one
whose takeover is recorded (§12.2) **and** the record's own store-relative path is listed in that
generation's `generation-fence` — the fence of the attempt it belongs to for an attempt-level kind, the
store-root fence for a project-level one (§12.4). The second clause is what distinguishes a record that
was already there when this owner took over from one a ghost wrote into an old generation afterwards;
both carry the same generation and the same run, so nothing else can tell them apart.

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
├── control/
│   ├── stop-requests/<NNNN>.json            # a stop that survives a restart, §10.1
│   └── clearances/<NNNN>.json               # the only thing that lifts one, §10.1
├── generation-fences/<generation>.json      # what the project-level store held at takeover, §12.4
├── attempts/<attempt-id>/
│   ├── reservation.json                     # identity, written before the grant exists
│   ├── prepared.json                        # the contract, §9.1
│   ├── scopes/<scope-id>.json               # one per execution scope, before it may write, §6.1
│   ├── launch.json                          # start outcome and handle
│   ├── result.json                          # the worker's manifest
│   ├── sealed/<scope-id>.json               # one per execution scope, §6.1
│   ├── captures/<check-id>/<NNNN>-<capture-id>.json   # what was observed, §11.2
│   ├── assessments/<check-id>/<NNNN>.json   # whether it was adequate, and who judged it, §11.5
│   ├── logs/<check-id>/<NNNN>.out           # truncated to config.log_cap bytes
│   ├── logs/<check-id>/<NNNN>.err
│   ├── classification.json
│   ├── holds/<cause-id>.json
│   ├── resolutions/<cause-id>.json
│   ├── stop-evidence.json
│   ├── generation-fences/<generation>.json  # what this attempt's store held at takeover, §12.4
│   ├── mutations/<receipt-id>/
│   │   ├── intent.json                      # one canonical mutation this attempt intends, §8.3
│   │   ├── fences/<NNNN>.json               # the revision, run and generation one submission expected
│   │   └── ack.json                         # the receipt read back after that mutation landed
│   └── release.json
└── runtime/
    ├── heartbeat/<attempt-id>.json          # advisory, overwritten in place, never journaled
    └── projection.json                      # a human-readable mirror; no decision reads it
```

Three of those paths are new in revision 6, and each replaces a path that could not hold what was
being written into it.

- **`mutations/<receipt-id>/`, replacing `acceptance.json` and `commit-observed.json`.** An attempt
  performs up to two canonical mutations — one to mark the task `RUNNING` at dispatch (§8.1, O4) and one
  to mark it `DONE`, `BLOCKED` or `SKIPPED` at completion (O11) — and revision 5 gave both the same
  immutable filename. Publish-if-absent then made the second one a `conflict` against the first, so the
  design's own primitive forbade its own operation sequence. A mutation is now identified by its
  `receipt_id`, which is derived from the intent and the evidence (§7.4), so the two mutations have
  different receipt ids by construction: one carries intent `RUNNING` with an empty snapshot and no
  qualifying captures, the other a terminal intent with both. **`mutation_id ≡ receipt_id`**; there is
  no second identifier.
- **`fences/<NNNN>.json` under a mutation.** A submission that loses a revision race is not a different
  intent, and revision 5 had nowhere to record the second attempt at the same one. The intent record
  therefore no longer carries `expected_revision`; each submission publishes a numbered `fence` record
  instead. A revision conflict means revalidate and publish fence *N+1* under the same receipt id;
  evidence that changed means a *different* receipt id, because the intent itself changed.
- **`assessments/<check-id>/<NNNN>.json`, carved out of the capture.** A capture is immutable and
  content-addressed, and revision 5 had the coordinator write `assessor`, `rationale` and `adequate`
  into it after the fact (§11.5). Those are now a separate immutable record (§7.4) that references the
  capture by id and digest, so neither record is ever rewritten.

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
     EEXIST -> read final; equal content_digest (§7.3) -> "identical"; different -> "conflict"
  5. fsync the directory containing final
  6. unlink tmp, then fsync its directory
  7. return "created"
```

Five properties, each of which an earlier revision stated incompletely.

- **`identical` is content identity, not byte identity.** Revision 5 compared bytes here while §7.4's
  common envelope carries `coordinator_run`, `ownership_generation` and a second-precision `written_at`.
  Those three are stamped by whoever writes, not derived from what is written, so a retry a second later
  — and, more sharply, a successor completing a predecessor's operation after a takeover — re-derives the
  same record and can never produce the same bytes. Byte comparison would therefore report `conflict` for
  exactly the completions §8.2 requires to be `created` or `identical`: its O3 row ("republishing
  `scopes/worker.json` is `identical` or `created`"), its O7–O11 row, and §8.3's first bullet. The
  comparison is over `content_digest` (§7.3), which excludes the writer-stamped envelope fields;
  `body_digest` stays whole-record because it is tamper evidence, not an equality test.
- **`link` is the no-clobber operation.** `os.replace` clobbers by definition [C9], so the primitive
  that makes a record immutable cannot be built from it.
- **`identical` still owes the barrier.** A predecessor may have linked `final` and died before step 5.
  An identical-content retry therefore performs steps 5–7 before returning `identical`; only then is the
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
  explicitly ordered there — never "some order". **The default, which applies to every array §7.4 does
  not give a rule for, is ascending**: strings by Unicode code point, and objects by the tuple of their
  identity fields in the order §7.4 lists them. Revision 5 required each array to be "explicitly
  ordered" and then left several of them saying only "ordered list", which is the same defect one level
  down; a default closes it without an audit.
- Integers only, written without exponent or fraction, for every field that enters a digest. No float
  ever does.
- **Exactly one trailing newline** (`\n`) after the closing brace. Not zero, not two, not "if any".
- `body_digest` is `sha256` over the canonical serialization of the record with **both** the
  `body_digest` key and the `receipt_id` key removed, hex-encoded lowercase. Removing `receipt_id` is
  what makes the digest computable: a `receipt_id` is itself a hash over part of the record (§7.4), so a
  record that carried one inside its own digest input would have no fixed point. Every other field is
  included. Unknown fields introduced by a higher *minor* version are preserved and **included** in the
  digest, so a v1.0 reader still recomputes what a v1.1 writer wrote; they are excluded from every
  decision.
- `content_digest` is `sha256` over the canonical serialization of the record with the `body_digest` and
  `receipt_id` keys removed **and** the writer-stamped envelope fields `coordinator_run`,
  `ownership_generation`, `writer` and `written_at` removed. It is a **comparison function, not a
  field**: no record carries it, and nothing is signed with it. It exists because two publications of
  one record must be allowed to disagree about who wrote them and when while agreeing about everything
  else — which is what §7.2 step 4 asks and what §8.2's completions after a takeover require. The two
  digests answer different questions: `body_digest` asks whether these bytes are the bytes that were
  written, `content_digest` asks whether these two records say the same thing. Unknown minor-version
  fields are included here for the same reason they are included in `body_digest`.
- `attempt_id` is `att-` plus 32 lowercase hex characters from the platform CSPRNG. It carries no
  meaning; task, generation and counter are fields.
- `capture_id` is `cap-` plus the first 32 characters of the capture's `body_digest`, making captures
  content-addressed with no self-reference. It is **not a field of the capture record**: it appears in
  the filename and in references to the capture, and a reader derives it from the bytes it just read.
  Revision 5 said both — §11.2's field table omitted it while §7.3 said it was "inside the record" — and
  storing it inside would have reintroduced exactly the self-reference this bullet exists to avoid. A
  reference naming `(check_id, sequence, capture_id)` therefore resolves to a path by pattern, without
  an index.
- **Identifiers that appear in paths are constrained, and encoded.** A `check_id`, `cause_id` or
  `scope_id` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` before it is used in a path; anything else is a
  plan defect refused at admission (`R-UNENUMERABLE`). Canonical task ids are *not* restricted this way
  [C1], so a `task_id` appears in a path only where §7.1 shows one and is percent-encoded there. Scope
  ids contain two characters a filename should not: `:` is written `%3A` and `#` is written `%23`, so
  scope `check:lint#0002` is the file `scopes/check%3Alint%230002.json`. The encoding is applied to the
  filename only; the record's `scope_id` field carries the unencoded value.
- `sequence` is a JSON integer from 1; the filename encodes it zero-padded to four digits. §11.4 caps
  executions of one check within one attempt at 16, so the encoding cannot overflow. Heartbeat records
  have no sequence at all — they are overwritten, not appended (§15).

### 7.4 Record schemas

There are **two envelopes**, not one. Revision 5 required `task_id` and `attempt_id` on every record,
which no project-level record has: an `owner` record belongs to a coordinator run, and a stop request
belongs to the project.

The **common envelope**, required on every record and validated before any field is read (§6.3):

| Field | Type | Notes |
|---|---|---|
| `record_kind` | string | one of the kinds below; an unknown kind is a validation failure |
| `schema_version` | string | `"1.0"`; major 1 required, minor forward-compatible per §7.3 |
| `project_id` | string | must match the project being projected |
| `coordinator_run`, `ownership_generation` | string, integer | the writer's run and generation (§12.4) |
| `writer` | string | `coordinator`, `worker` or `adapter` |
| `written_at` | string | RFC 3339 UTC with a `Z` suffix and second precision |
| `body_digest` | string | §7.3 |

The **attempt envelope** adds two fields, and is required on every kind stored under
`attempts/<attempt-id>/`:

| Field | Type | Notes |
|---|---|---|
| `task_id` | string | must match the task the attempt is for |
| `attempt_id` | string | must match the directory the record was read from |

Project-level records — `owner`, `stop-request`, `stop-clearance`, and the store-root
`generation-fence` — carry the common envelope only, and a projection of an attempt never reads them for
`attempt_id` agreement. `config.json` and
`plans/<task-id>.<plan-hash>.json` are not journal records and carry no envelope; they are immutable
per generation and validated by their own schemas (§7.6, §9.1).

Kind-specific fields, all required unless marked optional:

| Kind | Fields |
|---|---|
| `reservation` | `plan_hash`, `claims` (ordered list of §9.2 claim strings), `counter` (integer, attempts of this task so far) |
| `prepared` | `definition_hash`, `plan_hash`, `contract` (§9.1), `baseline` (map: claim string → digest or `absent`, covering every read claim, every write claim and every check subject, §9.4), `instruction_digest` (§9.1), `deadline_at` (RFC 3339 UTC), `deadline_budget_s` (integer > 0), `heartbeat_interval_s` (integer > 0), `log_cap_bytes` (integer > 0) |
| `scope` | `scope_id`, `kind` (`worker` or `check`), `check_id` (required when `kind` is `check`, otherwise absent), `sequence` (integer, required when `kind` is `check`), `claims` (ordered list of §9.2 claim strings this scope may write) |
| `launch` | `start_outcome` (`started`, `no_start`, `ambiguous`), `handle` (optional string, required when `started`), `capability_id`, `started_at` (RFC 3339 UTC), `adapter` (name and version) |
| `result` | `outcome` (`success`, `failure`, `refused`), `produced` (map: output claim → digest or `absent`), `baseline_matched` (bool: every read claim's digest still equals `prepared.baseline`), `captures` (ordered list of capture references `{check_id, sequence, capture_id}`), `summary` (string ≤ `config.summary_cap`), `refusal_code` (optional, required when `refused`) |
| `sealed` | `scope_id`, `exit` (§11.2 exit status variant), `tree_exited` (bool, must be true), `resumable` (bool, must be false) |
| `capture` | §11.2 |
| `assessment` | `check_id`, `sequence`, `capture_id`, `capture_body_digest`, `assessor` (`{kind, identity}`; `kind` is `exit_status`, `coordinator` or `human`, per §11.5), `adequate` (bool), `rationale` (string) |
| `classification` | `class` (one of §14.1's classification classes), `evidence` (ordered list of capture references), `conflict_digests` (optional) |
| `hold` | `cause_id`, `cause_class` (one of §14.1's fourteen), `detail` (string), `raised_by` (`coordinator` or `operator`) |
| `resolution` | `cause_id`, `resolution` (`rerun`, `accept`, `retry`, `block`, `skip`, `withdraw`, `stop`), `observed_revision` (integer: `project.json`'s revision when the disposition was decided, §14.3), `decided_by`, `rationale`, `evidence` (optional ordered list) |
| `stop-evidence` | `kind` (§13), `observed_at`, `detail`, `scopes` (ordered list of scope ids this evidence covers) |
| `acceptance` | stored as `mutations/<receipt-id>/intent.json`. `intent` (`RUNNING`, `DONE`, `BLOCKED`, `SKIPPED` or `TODO`; it is the durable intent of *any* canonical mutation, §8.1), `receipt_id`, `definition_hash`, `accepted_snapshot` (map: output claim → digest or `absent`), `qualifying_captures` (ordered list of capture references) |
| `fence` | stored as `mutations/<receipt-id>/fences/<NNNN>.json`. `receipt_id`, `sequence` (integer from 1), `expected_revision`, `expected_run`, `expected_generation`, `submitted_at` |
| `commit-observed` | stored as `mutations/<receipt-id>/ack.json`. `receipt_id`, `fence_sequence` (the fence that won), `committed_revision` (integer), `evidence_index` (integer, the position in the task's `evidence` array), `committed_status` (the task status read back) |
| `generation-fence` | stored as `generation-fences/<generation>.json`, once under each attempt and once at the store root for the project-level records. `generation`, `taken_over_from` (the predecessor's generation), `scope` (`attempt` or `project`), `records` (ordered list of store-relative record paths present when the takeover ran) |
| `release` | `receipt` (the terminal mutation's `receipt_id`, or null when no terminal mutation was committed), `sealed_scopes` (ordered list, must equal `declared_scopes`), `grant_removed` (bool), `reason` |
| `owner` | `host_id`, `boot_id`, `pid`, `process_start`, `started_at`, `generation` (§12.1) |
| `stop-request` | `sequence` (integer from 1), `requested_by`, `reason` |
| `stop-clearance` | `sequence`, `clears` (the `sequence` of the stop request it lifts), `cleared_by`, `rationale` |
| `heartbeat` | `phase` (free string), `observed_at`; D2 only, never journaled |

`receipt_id` is `rcp-` plus 32 hex characters of the `sha256` of the canonical serialization of
`{attempt_id, definition_hash, intent, accepted_snapshot, qualifying_captures}`, and it is exactly the
mutation's identity: two submissions of the same intent over the same evidence produce the same
`receipt_id` and therefore the same directory, and any change to the intent or the evidence produces a
different one. It is excluded from `body_digest` (§7.3), and no other field name is used for it
anywhere in this document.

Three consequences are worth stating, because they are what makes §7.1's mutation directory work.

- The dispatch mutation and the completion mutation **cannot collide**. The first has `intent`
  `RUNNING`, an empty `accepted_snapshot` and an empty `qualifying_captures`; the second has a terminal
  intent and both populated. Different inputs, different `receipt_id`, different directory.
- A **retry of the same intent** publishes another fence, not another intent. `expected_revision` is a
  property of one submission, not of the mutation, which is why it moved out of the intent record; a
  coordinator that loses a revision race revalidates and publishes `fences/0002.json`.
- The word `acceptance` is a **misnomer kept on purpose**. The kind is the durable intent of any
  canonical mutation, including the one that marks a task `RUNNING`, which accepts nothing. Renaming it
  would move I3, the §6.2 `ACCEPTED` row and §11.3's receipt tuple for no change in behaviour; the name
  is a historical artefact of revision 4 and is documented rather than churned.

### 7.5 Durability classes and the failure model

| Class | Records | Barrier | Survives |
|---|---|---|---|
| D1 | every kind in §7.4 except `heartbeat`, plus `plans/`, `config.json`, captures and logs; the project-level kinds (`owner`, `stop-request`, `stop-clearance`) are D1 too, which is what makes a stop request survive a restart (§10.1) | §7.2 in full, including the barrier on the `identical` path | process crash, kill, host restart, and OS crash or power loss for any record whose step 5 returned |
| D2 | `heartbeat`, `runtime/projection.json` | none of §7.2's; overwritten in place with `atomic_write_json`, which does fsync the file and its parent [C9] | nothing that matters; both are rebuilt or discarded |
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
4. It commits the `execution` block, bumping `schema_version` from `"3"` to `"4"` and setting `updated`
   as any commit does. `IMMUTABLE_PROJECT_FIELDS` is unaffected: the block is new, not a change to
   identity [C21]. This is the **only** command that may change `schema_version`, and it does nothing
   else: it takes the project lock, writes that one field and that one object, and returns. Every
   ordinary commit — including every canonical mutation of §8 — refuses a candidate whose
   `schema_version` differs from the stored one, so the version cannot be carried along as a side
   effect of a task transition, and a downgrade cannot be smuggled through a retry of a stale
   candidate. A single-purpose command is also what makes the change auditable: one revision in the
   project's history contains the activation and nothing that could be confused with it.
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

That gate is reachable only because a coordinator can put `coordinator_run` back to null. Revision 5
had no operation that did, so a project that had ever run one attempt could never close — the gate
was unsatisfiable rather than strict. `O20 relinquish` (§8.1) is that operation: its precondition is
exactly the first three clauses of this gate, and its effect is to release ownership without touching
the generation.

### 7.7 Retention and collection

Retention is **whole-attempt or nothing**. Collection removes an attempt's directory entirely, and
only when its label is `RELEASED`, its task is terminal, and the closure gate above passes. Nothing
inside a retained attempt is ever selectively deleted: every record listed in §7.4 stays, including
each `fence` of each mutation and each `assessment`, because a mutation's directory read without its
fences no longer shows how many submissions it took, and a capture read without its assessment no
longer shows who judged it adequate. The only thing truncated is a capture's log body, to
`config.log_cap`.

Revision 5 described collection as preserving an enumerated subset, which invited exactly the reading
this paragraph forbids: a reader cannot tell a record that was never written from one that a
collection pass judged uninteresting. An enumerated subset is also a list that goes stale every time
§7.4 gains a kind — it had already gone stale, listing neither `fence` nor `assessment`. Retention of
the `plans/` entry and `config.json` for the generation is on the same footing: the
`definition_hash` and `plan_hash` a receipt carries are auditable only while the definitions they
name are present, so the plan is retained rather than the hash alone.

## 8. Operations

### 8.1 The operation table

Twenty operations. Each has a **precondition** over the facts of §6.1 — never over a label — a
**canonical effect** stated as the transitions it commits, and an ordered **durable effect sequence**
whose every prefix §8.2 handles. The canonical column reads `none` when the operation writes only to
the journal or the registry.

Every canonical mutation follows the same four-phase shape, in this order and no other:

```text
intent  ->  mutations/<receipt-id>/intent.json, published if absent
fence   ->  mutations/<receipt-id>/fences/<NNNN>.json, carrying expected_revision/run/generation
commit  ->  research-project commit --expected-revision, under .project.lock, fenced per §12.3
ack     ->  mutations/<receipt-id>/ack.json, written only after re-reading project.json and
            finding the receipt
```

Four phases, not three, and the split is the point. The intent is a property of the mutation and is
therefore immutable and content-addressed; the fence is a property of one *submission* and there may
be several. A coordinator that loses a revision race revalidates (§8.3) and publishes the next fence
under the same `receipt_id`; a coordinator whose revalidation changes the evidence computes a
different `receipt_id` and starts a different mutation directory. Revision 5 wrote both facts into a
single `acceptance.json` per attempt, which made a second submission a `conflict` against the first
under its own publish-if-absent rule (§7.2) — the design forbade its own retry path — and made the
same file the only home for both the dispatch mutation (`RUNNING`) and the completion mutation.

| Id | Operation | Precondition over facts | Canonical effect | Durable effects, in order |
|---|---|---|---|---|
| O1 | `reserve` | task admissible (§10.1); `bound_attempt` is null; no attempt of this task is unreleased | `none` | 1 publish `reservation` |
| O2 | `grant` | `reserved`; not `grant_present` | `none` | 1 under the registry lock: re-scan every grant, verify no conflict (§9.3), insert the grant with `capability` = `none` |
| O3 | `prepare` | `reserved`; `grant_present`; the plan's `plan_hash` still matches `plans/` | `none` | 1 read the baseline digests of every read claim, every write claim and every check subject (§9.4); 2 publish `scopes/worker.json`; 3 publish `prepared` |
| O4 | `dispatch` | `prepared`; `authorization_in_force`; `task_status` is `TODO`; `open_causes` empty; not `stop_requested` (§10.1); capacity (§10.3); the dispatch gate open (§14) | `` `TODO → RUNNING` `` | 1 intent with `intent` `RUNNING`, 2 fence, 3 commit binding `execution.attempts[task_id]`, 4 ack; 5 under the registry lock set `capability` = `issued` with a fresh `capability_id`; 6 call the adapter's `start`; 7 publish `launch` with the outcome; 8 under the registry lock set `capability` = `consumed` |
| O5 | `record-result` | `launch_capability` is `issued` or `consumed`, or `uncertain_start`; written by the worker or by the inline executor | `none` | 1 fsync every declared output and its parent; 2 publish `result` |
| O6 | `seal` | the adapter reports the scope's process tree exited and the host cannot resume it | `none` | 1 publish `sealed/<scope-id>` |
| O7 | `classify` | `sealed_scopes` contains `worker`; `result` non-null, or `stop_evidence` non-null, or the deadline passed | `none` | 1 validate every capture; 2 publish `classification` |
| O8 | `hold` | a cause of §7.4's `cause_class` is detected; no other precondition, and none on the label | `none` | 1 publish `holds/<cause-id>` |
| O9 | `resolve` | `open_causes` contains this `cause_id`; a `human_review` cause resolves only on an `assessment` meeting §11.5 | `none` | 1 publish `resolutions/<cause-id>` |
| O10 | `run-check` | `prepared`; the grant holds the check's declared read **and** write claims; the check's `executor` (§9.1) is this executor | `none` | 1 publish `scopes/check:<id>#<NNNN>.json` — the first durable effect, before any process runs; 2 run the check; 3 publish the capture; 4 publish the assessment (§11.5); 5 publish `sealed/check:<id>#<NNNN>` |
| O11 | `accept` | `open_causes` empty; `open_scopes` empty; `classified`; adequacy holds (§11.4); every qualifying capture's after-snapshot equals the `accepted_snapshot` (§11.3) | `none` | 1 publish the intent |
| O12 | `commit-acceptance` | `accepted`; the re-derived `definition_hash` still equals the intent's | `` `RUNNING → DONE` ``, `` `RUNNING → BLOCKED` ``, or `` `RUNNING → SKIPPED` `` | 1 append the receipt to `evidence.md`; 2 publish the fence; 3 commit the complete candidate (§8.4); 4 ack |
| O13 | `withdraw` | some cause of this attempt has a `resolution` whose disposition is `withdraw`, or authorization is no longer in force; `open_scopes` empty | `` `RUNNING → TODO` ``, `` `RUNNING → BLOCKED` ``, or `none` | 1 publish the intent with `intent` `TODO` or `BLOCKED`; 2 publish the fence; 3 commit; 4 ack |
| O14 | `retry` | `task_status` is `BLOCKED`; `open_causes` empty; every attempt of this task released | `` `BLOCKED → TODO` `` | 1 publish the intent with `intent` `TODO` on a new attempt; 2 publish the fence; 3 commit, clearing `block_reason`; 4 ack |
| O15 | `stop` | an operator asked, or the deadline passed | `none` | 1 publish `control/stop-requests/<NNNN>.json`; 2 publish `holds/<cause-id>` with `cause_class` `operator_stop`; 3 call the adapter's `terminate` for every open scope; 4 publish `stop-evidence` naming the scopes it covers; 5 O6 for each scope the evidence establishes |
| O16 | `release` | `sealed_scopes` equals `declared_scopes`; `launch_capability` is `consumed` or `revoked`; `open_causes` empty; `accepted` implies `commit_observed`; not `indeterminate` | `none` | 1 publish `release` with `grant_removed` false; 2 under the registry lock remove the grant; 3 publish nothing further — step 1 is the authorization, step 2 the act |
| O17 | `take-over` | the predecessor is proven dead (§12.2) | `none` | 1 commit the new `coordinator_run` and `ownership_generation`; 2 publish `generation-fences/<generation>.json` for every attempt in the store **and** one at the store root covering the project-level records; 3 delete `runtime/`; 4 reproject every attempt; 5 for each, resume at §8.2's completion for its prefix |
| O18 | `dispose` | `launch_capability` is `none`; every open scope is sealable; not `indeterminate` | `none` | 1 O6 for every open scope with exit variant `not_started`; 2 publish `release` with `receipt` null; 3 under the registry lock remove the grant |
| O19 | `acquire` | `coordinator_run` is null | `none` | 1 publish `owners/<coordinator-run>.json`; 2 commit `coordinator_run` = this run and `ownership_generation` = `G+1`, fenced on `expected_run` null and `expected_generation` `G` (§12.3) |
| O20 | `relinquish` | no `grant_present` in the registry, every attempt in the store has `released` or no records at all, and `execution.attempts` is empty | `none` | 1 commit `coordinator_run` = null, leaving `ownership_generation` at `G` |

`O18 dispose` exists because the attempts that must be torn down before anything started are exactly
the ones O16 cannot touch: O16 requires `launch_capability` to be `consumed` or `revoked`, which a
never-dispatched attempt's is not. Revision 5 routed those prefixes to O16 anyway, so its own
precondition refused them and the attempt was unreleasable — which, by §7.6's closure gate, meant the
project could not close. `O18` takes the complementary precondition (`launch_capability` is `none`),
seals the declared scopes with the exit variant that says truthfully that nothing ran (§11.2's
`not_started`), and removes the grant.

`O19 acquire` and `O20 relinquish` exist because §12's ownership rules described a generation counter
that nothing was allowed to establish or clear. Revision 5's O17 `take-over` could only *succeed* a
predecessor, so a project with `coordinator_run` null had no legal path to a first coordinator, and one
with a coordinator had no legal path back to null. §12.3 now states the fence and the candidate
separately for exactly these three operations, because they are the only ones where the values a
coordinator reads differ from the values it writes.

Three ordering questions revision 4 answered twice, answered once here.

- **Release order.** `release` is published **before** the grant is removed, and its `grant_removed`
  field is therefore false in the record. The registry is the authority for admission — an unremoved
  grant keeps excluding writers, which is the safe direction — and the journal is the authority for
  *authorization* to remove it. A projection that finds `release` present and the grant still there
  reports the grant as held and completes step 2; it never reports the claim as free.
- **Acquisition order.** `reservation` precedes the grant, so a grant with no `prepared` is always
  reconcilable: the reservation names the task, the plan hash, the claims, and the counter. A
  reservation with no grant holds nothing. Both prefixes appear in §8.2.
- **Intent before commit, everywhere.** Dispatch, acceptance, withdrawal and retry all use the same
  four-phase shape, so no operation orders its journal record after its commit and no crash leaves a
  canonical status whose journal has no explanation. O19 and O20 are the two exceptions, and they are
  exceptions because they mutate no task: O19's `owner` record precedes its commit for the same reason,
  and O20 has no record to write because relinquishing ownership asserts nothing about any attempt —
  its precondition is that there are none.

### 8.2 Crash prefixes and their completions

A crash prefix is a proper prefix of an operation's durable effect sequence. Recovery (§13) enumerates
active attempts, computes facts, and for each prefix below performs the completion — which is itself an
operation, so a crash during recovery is just another prefix.

| Operation | Prefix | What the store shows | Completion |
|---|---|---|---|
| O1 | after 1 | `reserved`, no grant | O2, or abandon with O18 — which is legal here because `launch_capability` is `none` and no scope is open |
| O2 | after 1 | grant, no `prepared` | O3 if the plan still matches; else O18 |
| O3 | between 1 and 3 | grant, no `prepared`; the baseline read or the scope publication may have failed | identical to the O2 prefix: O3 is retried from the reservation, a second baseline read is legal because nothing has been dispatched, and republishing `scopes/worker.json` is `identical` or `created` |
| O4 | after 1 | the `RUNNING` intent, no fence | publish the fence and continue; the intent is idempotent by `receipt_id` |
| O4 | after 2 | intent and fence, nothing committed | re-run step 3 against the fence just read; if the revision has moved, publish the next fence |
| O4 | after 3 | canonical `RUNNING`, no ack, `capability` `none` | re-read `project.json`; if the binding is present, ack and continue at step 5 |
| O4 | after 5 | `capability` `issued`, no `launch` | **ambiguous**: the adapter may or may not have started. Publish `launch` with `start_outcome` `ambiguous`, set `uncertain_start`, and go to §13.2. Never re-issue the capability |
| O4 | after 6, before 7 | the adapter started something; nothing recorded | the same ambiguous path; the handle is unknown, so §13.2's discovery is the only route |
| O4 | after 7 | `launch` present, `capability` still `issued` | set `capability` `consumed` under the registry lock; the fact was already durable |
| O5 | after 1 | outputs on disk, no `result` | wait for the worker, or on stop evidence classify from what is there (§13.3) |
| O7–O11 | any | one journal record missing | re-run the operation; publish-if-absent makes a re-run either `created` or `identical` |
| O10 | after 1 | a `scope` record, no capture | the scope is open and O16 is blocked until it seals. Re-run the check, or on stop seal it with the exit variant the evidence supports |
| O12 | after 1 | receipt in `evidence.md`, no fence | not proof of anything (I4): publish the fence and continue. The evidence append is deduplicated by `receipt_id`, so it is written at most once |
| O12 | after 2 | intent and fence, not in `project.json` | re-run step 3. If the revision moved on, revalidate (§8.3): unchanged evidence means the next fence under the same receipt, changed evidence means a different receipt |
| O12 | after 3 | committed, no ack | re-read `project.json`; if the receipt tuple is at `evidence_index`, ack. If the revision moved on and the receipt is absent, the commit did not land: re-check O12's precondition and retry |
| O13, O14 | any | as O12 | identical, because all three share the intent/fence/commit/ack shape |
| O15 | after 1 | a durable stop request, scopes open | continue at step 2. The request is what makes the stop survive a restart, so a coordinator that recovers here does not resume dispatching (§10.1) |
| O15 | after 2 | `operator_stop` cause, scopes open | call `terminate` again; it is idempotent by handle |
| O15 | after 4 | stop evidence, unsealed scopes | O6 for each scope the evidence covers; a scope the evidence does not cover stays open, and O16 stays blocked |
| O16 | after 1 | `release` present, grant present | remove the grant. This is the only prefix in which the journal is ahead of the registry, and it is safe in that direction |
| O17 | after 1 | new generation, some generation fence missing | continue at step 2, publishing whichever fences are absent. Until all of them are written, §6.3 cannot decide whether an earlier-generation record was present at takeover, so every attempt whose fence is missing projects `indeterminate`, and while the store-root fence is missing `stop_requested` is `indeterminate` too (§12.4) |
| O17 | after 2 | new generation, old `runtime/` | continue at step 3 |
| O18 | after 1 | scopes sealed `not_started`, no `release` | continue at step 2 |
| O18 | after 2 | `release` present, grant present | remove the grant, exactly as the O16 prefix |
| O19 | after 1 | an `owner` record for a run that owns nothing | re-run step 2. If the commit finds `coordinator_run` non-null, another coordinator won: this run is not the owner, and the orphan `owner` record is evidence of a lost race, not of ownership |
| O20 | after 1 | nothing; the operation is one commit | none. O20 has no prefix, which is why it is safe to run at closure |

### 8.3 Replay and idempotence

- Every D1 publication is idempotent by name: a re-run returns `created` or `identical`, and
  `identical` completes the barrier (§7.2). Idempotence here is over the record's *content*, not its
  bytes: a re-run stamps its own `coordinator_run`, `ownership_generation` and `written_at`, so the
  comparison that decides `identical` is `content_digest` (§7.3). This is what lets a successor complete
  a predecessor's half-finished operation at all — every completion in §8.2 republishes records the dead
  run derived, under the successor's run and generation.
- Every canonical mutation is idempotent by **receipt identity**, never by status equality. `TODO` is
  not proof that *this* attempt's withdrawal was applied — a later retry of the same task produces the
  same status — so O12, O13 and O14 all look for their own `receipt_id` in the task's `evidence` array
  before concluding the mutation landed.
- That lookup is **operation-aware**, because `bound_attempt` means different things on either side of
  the commit. Before the commit, `bound_attempt` must equal this `attempt_id`: a mutation submitted
  against a binding that is not this attempt's is submitting on someone else's behalf and is refused.
  After the commit, `bound_attempt` proves nothing — O12's terminal mutations clear the binding, and
  O13's `RUNNING → TODO` clears it too — so success is proved by the receipt tuple standing at
  `evidence_index` and by nothing else. Revision 5 stated the pre-commit rule as though it also held
  afterwards, which would make every successful terminal mutation read as not having landed.
- **Revalidation on a lost revision race** is a decision between two outcomes, and it is made by
  recomputing the intent, not by re-reading a status. The coordinator recomputes
  `{attempt_id, definition_hash, intent, accepted_snapshot, qualifying_captures}` against the new
  revision. If the recomputed `receipt_id` is unchanged, the evidence still supports the same mutation
  and the coordinator publishes the next fence under the same directory. If it differs, the world moved
  under the mutation: the old intent is abandoned in place — it stays as the record of what was once
  intended — and a new mutation directory is opened. No record is ever rewritten to make a retry fit.
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
removes `execution.attempts[task_id]` to match the binding the mutation intends, leaves
`IMMUTABLE_PROJECT_FIELDS` alone [C21], and copies `schema_version` through unchanged — a candidate
whose `schema_version` differs from the loaded revision's is refused, because only
`enable-execution` may change it (§7.6). The candidate is then dry-run through the real validator before
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
| `instruction` | the exact text the worker is given: what to do, which paths it may write, and the contract it must return. Required and non-empty — without it the plan resolves how a task is *verified* and never says how it is *performed*, which is the one thing a worker cannot infer |
| `checks[]` | ordered; each `{check_id, kind, argv or criteria, cwd, expect_exit, subjects[], reads[], writes[], executor}`. `check_id` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (§7.3). `executor` is `worker` or `coordinator` and says which of the two runs it: a `criteria` check whose only available executor would be the coordinator itself is refused with `R-SELF-ASSESSMENT` (§11.5) |
| `reads[]` | every path the **task** reads, including dependency outputs it consumes; the task's optional v4 `reads` declaration must enumerate this set explicitly |
| `writes[]` | every path the task writes, which must equal the union of its declared `outputs` |
| `enumerable` | bool; false means `R-UNENUMERABLE`, which is not admitted (§4) |
| `definition_hash` | see below |

`checks[].mutation_policy` appeared in revision 5's tuple and was defined nowhere in the document: no
section gave it a value space, a default, or a consequence. It is deleted rather than specified,
because a check either declares write claims or it does not, and that declaration is what §9.3
arbitrates. A field that only a reader's imagination can fill is worse than an absent one.

`definition_hash` is defined in exactly one place, here, as the `sha256` of the canonical serialization
of this object and nothing else:

```json
{"authorization": {"required": true, "scope": "..."},
 "checks": [...],
 "cwd": "<the resolved absolute working directory>",
 "depends_on": ["..."],
 "effect": {"kind": "local_write"},
 "instruction": "...",
 "outputs": [{"root": "target", "path": "...", "required": true}],
 "reads": ["..."],
 "success_criteria": "...",
 "task_id": "...",
 "verification_text": "...",
 "writes": ["..."]}
```

Four fields in it are there because an earlier revision omitted them and each can change what an
identical task description means: the resolved `cwd` (identical relative paths denote different files
under a different working directory), the resolved `checks` array (which is what `verification_text`
was interpreted to mean), `outputs[].required`, which the canonical output object requires [C26] and
which decides whether a missing output is a failure, and `instruction`, because two workers given
different instructions for the same task are not performing the same definition and their receipts
must not be interchangeable. Attempt identity (`attempt_id`), the baseline snapshot, and host
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
is why v1 refuses the shapes whose declarations cannot be complete (`R-UNENUMERABLE`), quiescing to the
v3 sequential path (§4), rather than comparing partial claim sets and calling the comparison
prevention. The residual is that a write to a path
nobody declared conflicts with nothing, is granted by nothing, and is seen by nothing
[UNENFORCED U1]:
nothing in this protocol prevents an undeclared write.

Three derivations that revision 4 got wrong or omitted:

- **A repository operation that changes the working tree claims the working tree.** `git switch`,
  `git rebase` and `git checkout` update the index and the working tree, not only `.git`, and `.git` and
  `src/a.py` are siblings, so a `.git` write claim does not conflict with a scoped writer. Such a task
  claims `local:<repository root>` as a write, which conflicts with every path under it, or it is not
  enumerable and is refused (§4).
- **Every check declares its writes.** A test runner writing `__pycache__`, a linter writing a cache, a
  build writing artifacts: each is a `writes[]` entry on that check, part of the attempt's grant, and
  part of the conflict relation. A check whose writes cannot be listed makes the plan not enumerable.
- **Protocol-owned paths are reserved.** `project.json`, `spec.md`, `evidence.md`, `briefing.md`,
  `reflection.md`, `INDEX.md`, `MEMORY.md`, `POSTMORTEMS.md`, the `execution/` subtree, and the
  registry are
  coordinator-only. Derivation **rejects** a worker claim naming any of them, any ancestor of any of
  them, or any descendant of `execution/` outside that attempt's own `store:` namespace. A task whose
  declared output is one of these is refused at admission rather than handed to a worker with
  contradictory instructions; the coordinator writes them itself.

### 9.4 Baselines and produced sets

`prepared.baseline` maps **every read claim, every write claim and every check subject** to the digest
of its contents, or `absent`. It is immutable for the attempt's life and is the reference for §11.3's
binding and §13.3's reconciliation.

Revision 5 covered read claims only, and excluded any path the task also writes. That made the baseline
unable to answer the two questions it exists to answer. §9.4's `created` / `modified` / `unchanged`
tags below are defined as a comparison of `result.produced` against "before", so a write claim with no
baseline entry has no before and cannot be tagged at all; and §11.3 binds a capture to the bytes that
were checked, which for a check subject the task writes is precisely the interesting case. A baseline
entry is a **record of what was observed**, not a promise that the path will not change: `modified` is a
legal tag and `result.baseline_matched` reports only whether the *read* claims still hold their baseline
digests. Nothing is asserted to be unchanging by being measured.

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
6. The task's `effect.kind` is `none` or `local_write`. `destructive` and `external` are refused with
   `R-EFFECT-NOT-CONFINED` and quiesce to the v3 sequential path (§4). There is no confinement field to
   check: revision 5 required `effect.confined_to` to cover every declared write claim, and the shipped
   validator accepts only `kind` and `description` on an effect object [C32], so a task carrying that
   field cannot be committed and a task without it can never satisfy the clause. The clause a validator
   can actually enforce is the kind, and the write claims are arbitrated by §9.3 where the conflict
   relation lives.
7. The plan is enumerable: every check's command decomposes under §9.1's rule and every subject,
   read and write claim is a concrete path. Otherwise `R-UNENUMERABLE`, and the task is not admitted.
8. Every `required: true` output is a `subject` of at least one check. Otherwise `R-CHECK-UNCOVERED`,
   and the task does not run at all.
9. No claim conflicts (§9.3) with a claim held by any attempt that is not `RELEASED`, including
   attempts of other projects sharing the workspace, whose claims are read from their grants (§10.4).
10. Capacity permits one more attempt (§10.3).
11. Not `stop_requested`.

Clauses 1–8 are properties of the project and the plan and are stable for the scan; clauses 9 and 10
are properties of the world and are re-tested under the registry lock at grant time (O2), because
between the scan and the grant another executor may have taken the resource. **The scan is advisory;
the grant is authoritative.** A design that treats the scan as the decision has a race whose window is
exactly the time the coordinator spends preparing.

Admission produces a refusal code, not silence. A task that fails clause 7, 8 or 6 is reported with
its code at the moment it is considered, so an operator reading the console sees `R-UNENUMERABLE` next
to the task that caused it rather than inferring it from a serialized run.

**`stop_requested` is durable, and that is clause 11's whole purpose.** It is a projected fact, not a
process variable: it is true when `execution/control/stop-requests/` holds a `stop-request` record whose
`sequence` is not the `clears` value of any `stop-clearance` under `execution/control/clearances/`.
Both kinds are D1 (§7.5), so a stop survives a coordinator crash, a restart, and a takeover by a
different coordinator run. Revision 5 held the stop in the running process, which meant the operator's
stop was undone by the crash it was often issued in response to — a coordinator restarted after a stop
resumed dispatching, with nothing in the store recording that it should not have.

While `stop_requested` holds, the ready loop **settles only**: it runs recovery, records results,
classifies, accepts, commits and releases, and it dispatches nothing. It closes when no scope is open
and no mutation directory lacks its `ack.json`. Lifting the stop requires publishing a clearance, which
is a deliberate, recorded act by whoever lifts it — not the absence of a flag, and not the side effect
of starting a new process.

### 10.2 The ready loop

```
loop:
  facts   = project(every unreleased attempt)                    # §6.1
  settle  = attempts with a completable prefix                   # §8.2
  if settle: complete one, continue                              # progress beats new work
  if stop_requested:                                             # §10.1 clause 11, durable
      if no scope is open and every mutation has its ack: close   # §13
      wait(poll_interval); continue                              # settle only; never dispatch
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
  dependant count, no duration estimate: at the widths §3.2 measured, where 202 of 331 dependency
  levels hold a single task, the difference between orderings is below what that sample can resolve,
  and an unmeasurable heuristic is a source of nondeterminism in the model for no gain.
  What plan order buys is **determinism, not fairness**. Two passes over the same facts produce the
  same `ready` sequence, which is what lets §21.2 assert on it. It does *not* prevent starvation:
  a task whose write claims conflict with a long-running attempt is passed over on every pass until
  that attempt releases, and a task refused for `R-CAPACITY` is passed over while any earlier-indexed
  ready task keeps taking the slot. Nothing here ages a waiting task, bounds how long it waits, or
  promotes it for having waited. Revision 5 claimed fairness followed from rebuilding the list in the
  same order; it does not — an order that is stable is exactly an order that keeps making the same
  choice. Ageing and priority are deferred (§22).
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

`capacity_free()` counts, against `max_concurrent` (§15.1), **one fact and nothing else**: every
attempt whose `grant_present` is true. Equivalently, every attempt whose grant is still in the registry,
which O16 removes in the same operation that publishes `release` (§8.1) — and, before any launch, O18.

Counting the fact rather than a set of labels is the fix for two defects revision 5 carried. Its list
enumerated nine labels and omitted `ACCEPTED`, `COMMITTED` and `INDETERMINATE`, all three of which hold
a grant — so an attempt that had been accepted but not yet released was free, and a coordinator at a
bound of four could hold seven grants. And enumerating labels contradicted §6.2: a label is a display
derivation, and a bound derived from a display is a bound that changes when the display does. The
reason the old list *wanted* every one of those labels is exactly `grant_present`: a reserved attempt
holds claims and will consume a slot, an `UNCERTAIN` one may have a live process, and an `ACCEPTED` one
still owns its writes.

The coordinator's own inline work is counted the same way and needs no separate rule: an inline task is
an attempt with a grant like any other. The rule used to be stated separately because a design that
treats inline work as free reaches `max_concurrent` + 1 concurrent processes; under `grant_present`
there is no way to express that mistake.

The bound is a **configured setting, default 2, ceiling 4**, and it carries no optimality claim. §3.2
withdraws the argument that the mean of per-project maximum widths justifies 4: a mean of maxima is not
a bound on demand. What the sample supports is that a cap of 2 already shows most of the modelled win,
so 2 is where a pilot starts (§20, Phase 6) and 4 is the highest value this design has thought about at
all — above 4 the registry, the retention rules and the heartbeat arithmetic of §15.2 have not been
sized. A grant refused for capacity is `R-CAPACITY`, the one refusal that resolves itself: the task
keeps its place in plan order and is re-scanned on the next pass, with no ageing (§10.2). The bound is
stored in `config.json` (§15) so raising it is a recorded operator decision rather than a code change;
§21.3 is where a different number would have to be earned.

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
- **A grant is a mutable file, not a journal record.** This is the one place in the design where a
  written file is replaced in place. O4 steps 5 and 8 change `capability` from `none` to `issued` to
  `consumed` on the same grant, so publish-if-absent (§7.2) cannot apply to it: a second `issued` state
  would be a `conflict` against the first. A grant is therefore written with `atomic_write_json` [C9]
  under `registry.lock`, with `body_digest` recomputed over the new contents, and the lock — not
  immutability — is what makes the sequence of states well defined. Nothing reads a grant as history:
  the journal is the history, and the grant answers exactly one question, which is whether the claims
  are held right now.

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
  `R-UNENUMERABLE` or `R-DIRECTORY-SUBJECT`. Neither is admitted (§4): the coordinator quiesces and the
  task is left to the v3 sequential path, outside this protocol.

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
| `criteria` | string or null | for `kind: criteria`, the resolved criteria text that was judged |

`assessor`, `rationale` and `adequate` are **not** fields of this record. They were in revision 5, and
they made the capture a mutable immutable: the capture is published by the worker at the moment the
check finishes, and the judgement is made afterwards by someone else, so filling those three fields in
later meant rewriting a D1 record whose `body_digest` had already been computed and possibly already
cited. §7.2's publish-if-absent would report the rewrite as a `conflict` against the record's own
earlier self.

The judgement is a separate immutable record, kind `assessment`, at
`attempts/<attempt-id>/assessments/<check-id>/<NNNN>.json`, carrying
`{check_id, sequence, capture_id, capture_body_digest, assessor, adequate, rationale}` (§7.4). Two
consequences follow, and both are improvements rather than costs. The assessment names
`capture_body_digest`, so a judgement is bound to the exact bytes it judged and cannot be silently
re-pointed at a rerun. And a capture may be assessed more than once — an operator overriding an earlier
coordinator assessment publishes the next `NNNN` — with the sequence, not an overwrite, recording that
the mind was changed. Adequacy (§11.4) is read from the **highest-sequence** assessment of a capture,
and a capture with no assessment is unassessed.

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
| `not_started` | none | the scope was declared and disposed before anything ran (O18); legal only while `launch_capability` is `none` |

Only `exited` with `code == expect_exit` can satisfy a `command` check. `signalled`, `timeout` and
`unknown` are **not** failures of the check: they are `indeterminate_check` causes (§14), because a
killed process says nothing about the property being checked. `spawn_failed` *is* a failure of the
plan, and raises a `plan_defect` cause rather than being retried: nothing about the environment will
make a missing executable appear. `not_started` never appears on a capture — a capture exists only
because a check ran — and appears only on the `sealed` record of a scope O18 disposed.

Separating the capture from the assessment is what replaces revision 4's single free `verdict` string,
which made "the check passed" and "someone decided the check passed" the same record. Now they are two
records, written by two writers, at two times.

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

with `qualifying_captures` the ordered list of capture references relied on. The receipt appears in
three places and means the same thing in each: as the name of the mutation's own directory
`mutations/<receipt-id>/`, in the task's `evidence` array as the canonical record, and in that
directory's `ack.json` as the confirmation that a re-read of `project.json` found it. Nothing infers a commit from a status: a `DONE` task whose `evidence` lacks
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

Every capture is judged by an `assessment` record (§11.2), and that record names its assessor. Three
kinds, and no others:

- `assessor.kind: exit_status` is not a judgement and carries no identity: it is the mechanical rule
  of §11.3 applied to a `command` capture, published by the coordinator as soon as the capture is
  validated. It never appears on a `criteria` capture. Its `rationale` is the exit variant restated,
  which is why a `command` check needs nobody's opinion.
- `assessor.kind: coordinator` carries `identity` = the coordinator's `coordinator_run`. It is
  permitted **only when the coordinator did not produce the bytes being judged** — that is, when the
  producing scope was a worker. The comparison is between `assessor.identity` and the producing
  scope's identity as recorded in `launch.json` and `result.json`, not between the strings
  "coordinator" and "worker".
- `assessor.kind: human` carries `identity` = the `source` recorded for the resolution, in the same
  form authorization uses [C23]. It is required for any `human_review` cause.

The rule is one sentence: **the producer may not be the assessor.**

Revision 5 drew a conclusion from that rule which its own §17.1 made unreachable. It said the `criteria`
checks of a task whose producer is the coordinator are not self-assessable, so the attempt holds a
`human_review` cause until an operator resolves it — and then §17.1 concluded that the host v1 first
runs on has no worker at all. Every `criteria` check of every task would therefore block on a human,
on every run, which is not a parallel executor; it is a queue with extra records.

v1 resolves this at **admission**, not at acceptance. `R-SELF-ASSESSMENT` refuses a plan containing a
`criteria` check whose `executor` (§9.1) can only be the coordinator itself, and the task quiesces to
the v3 sequential path (§4), where the coordinator judging its own work is what the user already has
and has already accepted. Nothing is admitted into this protocol that the protocol cannot verify. Three
things follow:

- A task whose verification decomposes entirely into `command` checks is unaffected, and §3.2's sample
  is dominated by such tasks: an exit status is a mechanical assessment with no producer to coincide
  with.
- A task with a `criteria` check and a real worker is unaffected: the worker produces, the coordinator
  assesses.
- A task with a `criteria` check and no worker is refused before it starts, with a code that names
  the reason, rather than dispatched and then held indefinitely. An operator who wants that task run
  under human assessment runs it the v3 way, which is exactly what refusing to the sequential path
  means.

`human_review` remains what §11.5 uses for a *dispatched* attempt whose judgement no permitted assessor
can make — a `criteria` check whose worker died before producing, most often — and §22 still defers the
general alternative of a second independent assessor process. What is withdrawn is the claim that a
universal human gate was a workable v1 policy.

A worker never assesses. It captures — it runs the command, records the exit variant, digests the
subjects, and publishes — and the coordinator publishes the assessment, which is why `capture` records
are worker-written and `assessment` and `classification` records are coordinator-written (§7.4).

## 12. Ownership, takeover, and fencing

### 12.1 Run identity

**The coordinator is a long-lived process.** It is a single OS process that owns the project for the
whole of an execution session: it starts, publishes its `owner` record, dispatches, waits for workers,
settles, and exits. It is not a sequence of short invocations that reconstruct their state from the
store, and every guarantee in this section depends on that. `pid_gone` (§12.2) is a proof only because
a run corresponds to exactly one process whose `pid` and `process_start` identify it; on a host where
the coordinator is a command that returns between operations, the pid is gone by design after every
operation and the proof degenerates into "the last command finished", which would authorise takeover of
a perfectly healthy session.

A host that cannot host such a process — one that offers no persistent process, or terminates the
agent between tool calls — refuses at activation with **`R-NO-RUNNER`** (§4). The whole protocol is
unavailable there: no execution store is created, `enable-execution` refuses, and the project stays on
the v3 sequential path. That is a narrower claim than revision 5 made, which described takeover proofs
without ever saying what a run *is*, and so implied a design that worked on hosts where `pid_gone`
means nothing. Phase 2 established that the supported Claude Code and Codex CLIs can host the POSIX
subprocess runner (§17.1); activation still probes the local runtime instead of trusting that result.

Three identifiers, with three different lifetimes:

| Identifier | Where | Changes when | Purpose |
|---|---|---|---|
| `coordinator_run` | `project.json.execution`, every record | a coordinator process starts and takes ownership (O19 or O17); back to null when it hands ownership back (O20) | says *who* wrote a record |
| `ownership_generation` | `project.json.execution`, every record | ownership is established (O19) or changes hands (O17), monotonically; unchanged by O20 | fences stale writers |
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

O17 is the only operation that raises `ownership_generation` **over a live predecessor's**, and its
precondition is a proof, not an inference. O19 also raises it, but only from a null owner, where there is
nothing to prove dead. Exactly three proofs are accepted, and each is recorded in the new owner record's
`took_over_from` object:

| Proof | Established by | Why it is a proof |
|---|---|---|
| `host_rebooted` | the predecessor's `owner` record has this `host_id` and a **different** `boot_id` | every process from the previous boot is gone, including descendants |
| `pid_gone` | same `host_id`, same `boot_id`, and no live process with the predecessor's `pid` **and** matching `process_start` | the pid is not merely absent but not reused; a matching start time would mean the process is alive. This is a proof **only** because §12.1 makes a run one long-lived process; on a host that refuses `R-NO-RUNNER` it is not one, which is why that host has no store to take over |
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

1. `--expected-revision <r>`: the on-disk `revision` must equal the value the fence names, which
   the existing command already enforces [C1].
2. `expected_run`: the on-disk `execution.coordinator_run` must equal the value the fence names.
3. `expected_generation`: the on-disk `execution.ownership_generation` must equal the value the fence
   names.

Revision 5 stated fences 2 and 3 as "the candidate's value must equal the on-disk value", which is a
rule three operations cannot obey: O17, O19 and O20 exist precisely to *change* those two fields, so a
candidate equal to what is on disk would make them no-ops. The fence and the candidate are two different
things, and this table states them separately. `R` is the run the coordinator holds, `G` the generation
it read, and `P` the predecessor's run.

| Operation | Fence: `expected_run` | Fence: `expected_generation` | Candidate: `coordinator_run` | Candidate: `ownership_generation` |
|---|---|---|---|---|
| O4, O12, O13, O14 and every other task mutation | `R` | `G` | `R` | `G` |
| O17 `take-over` | `P` | `G` | `R` | `G+1` |
| O19 `acquire` | null | `G` | `R` | `G+1` |
| O20 `relinquish` | `R` | `G` | null | `G` |

Read the table as one rule: **a coordinator fences on the world it read and commits the world it
wants.** For the four task mutations those are the same values, which is why revision 5's collapsed
statement looked correct; for the three ownership operations they differ, and the difference is the
operation's entire content. Fence 3 also explains why O20 leaves the generation alone: nothing
took over, so nothing stale needs fencing out, and a generation raised for a clean handback would make
every retained record from generation `G` unexplainable to §6.3.

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
- **A record from an explainable earlier generation is still not automatically trustworthy**, and this
  is the case revision 5 left open. "The takeover is recorded" tells a reader that generation `G-1`
  existed; it does not tell it whether *this* record was present when `G` took over or was written into
  `G-1` afterwards by a ghost. Both look identical: same generation, same run, an explainable takeover.
  O17 closes it by publishing, for every attempt it takes over, a `generation-fence` record at
  `attempts/<attempt-id>/generation-fences/<generation>.json` listing every store-relative record path
  present at that moment (§7.4). §6.3 then reads an earlier-generation record as history only if its
  path is in that generation's fence; a record from an earlier generation that the fence does not list
  did not exist at takeover, cannot have been written by the current owner, and sets `indeterminate`.
  The fence is written before `runtime/` is deleted and before any attempt is reprojected (§8.1, O17),
  so until it exists every attempt projects `indeterminate` and nothing dispatches.
- **The project-level records need a fence of their own**, and this is the case the paragraph above
  left open when it was first written. An attempt's fence can only list paths under that attempt, so a
  `stop-request` or an `owner` record from generation `G-1` would satisfy neither clause of §6.3 after
  a takeover and would be `indeterminate` for the rest of the project's life. That would break the one
  guarantee §10.1 asks of the stop gate — that a stop in force when a coordinator died is still in
  force for its successor — at exactly the moment it matters. O17 therefore publishes a second fence at
  the store root, with `scope` `project`, listing the project-level record paths present at takeover;
  §6.3 reads `owners/`, `control/stop-requests/` and `control/clearances/` against that one.
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
2. If ownership was established by O17, publish this generation's `generation-fence` for every attempt
   in the store, **and the store-root fence covering the project-level records**, *before* anything else
   reads a record (§12.4). Until they exist, every earlier-generation record is unexplainable: every
   attempt projects `indeterminate`, and so does the stop gate this scan is about to consult.
3. Delete `runtime/`. It is derived and its staleness is not worth reasoning about (§7.1).
4. For every attempt directory not `RELEASED`, and every id in `execution.attempts`, compute `facts`
   with validation (§6.3). An `execution.attempts` entry with no attempt directory is itself a fact —
   O4's prefix after 3 — and is completed, not ignored.
5. For every attempt, match its durable-effect prefix against §8.2 and perform the stated completion.
   Completions are operations, so a crash here leaves another prefix.
6. Reconcile claims: every unreleased attempt must have a grant, and every grant must name an
   unreleased attempt. A grant naming an attempt that is `RELEASED` is removed (O16 step 2). An
   unreleased attempt with no grant and no `prepared` is disposed as a bare reservation (O18); one with
   `prepared` or later and no grant is `indeterminate` — the record of what it was allowed to touch
   is gone, so nothing may be concluded about it.
7. Read `stop_requested` (§10.1). A stop that was in force when the previous process died is still in
   force now, and the ready loop settles without dispatching until it is cleared.
8. Rebuild `runtime/projection.json` for the human, and report every attempt with an open cause, every
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

**A cause class is not a classification class.** §7.4's `classification` record carries a `class`
field, and revision 5 pointed it at "one of §14's fourteen" — the cause classes above — which conflates
two different judgements. A cause says why an attempt may not proceed and is raised zero or many times;
a classification says what the attempt's run amounted to and is written exactly once, by O7, before any
disposition exists. The eight classification classes are:

| Class | Means |
|---|---|
| `success` | a `result` with `outcome` `success`, every qualifying capture adequate |
| `check_failure` | a `result` arrived and a qualifying capture is `adequate: false` |
| `indeterminate_execution` | a scope's exit was `signalled`, `timeout` or `unknown`, so the run says nothing |
| `no_result` | the scope sealed and no `result` record was ever published |
| `stopped` | O15 terminated the attempt; `stop_evidence` covers its scopes |
| `partial_write` | §13.3's reconciliation found changed declared writes with missing or inadequate checks |
| `plan_defect` | the plan could not be executed as written — `spawn_failed`, an unresolvable claim |
| `refused` | a `result` with `outcome` `refused`, carrying a `refusal_code` |

The two vocabularies overlap in three names (`check_failure`, `plan_defect`, and the `indeterminate`
prefix) because they describe the same underlying event from the two sides, and that is exactly why
stating one list and pointing at the other was a defect rather than a shorthand: a reader implementing
`classification.class` from §14.1's table would have accepted `operator_stop`, `unsafe_release` and
`stale_evidence` as classifications of a run, and would have had no class at all for a run that
produced nothing.

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
`decided_at` order and counting the trailing run of resolutions whose disposition is `retry` or `rerun`
**and whose `observed_revision` is at least the highest `committed_revision` among the project's
`DONE` acks**. When that run reaches `config.max_consecutive_failures` (default 3, §15), the
coordinator raises `operator_stop` and stops dispatching.

The reset event is exactly one thing: **an `ack.json` whose mutation's `intent` is `DONE`.** A task
that actually completed and was confirmed resets the run to zero. `BLOCKED` and `SKIPPED` acks do not:
they are also `commit-observed` records, and revision 5 reset on any of them, which made the budget
resettable by the very outcomes it exists to bound — three failed tasks blocked in a row would clear
the counter three times and the project would retry forever. Nothing else resets it either: not a
successful check inside a failing attempt, not the passage of time, not a `block` resolution. `block`
and `withdraw` do not extend the run; they end an attempt rather than retrying it, so they terminate the
run without resetting it.

The `observed_revision` clause is what makes the reset **durable rather than positional**. A budget
that counts "the trailing run of retry resolutions" alone is reset by any later `DONE` simply because
the `DONE` lands later in `decided_at` order — but resolutions written *before* that `DONE` and
resolutions written *after* it are indistinguishable once the run is recomputed from the store on a
fresh coordinator. Recording, on each resolution, the `project.json` revision the decision was made
against turns "after the last success" into a comparison rather than an ordering assumption, which is
why `resolution` carries `observed_revision` (§7.4).

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
| `max_concurrent` | 2 | the smallest bound above sequential, which §3.2 shows already carries most of the modelled win; 4 is the ceiling, and §10.3 claims no optimum |
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
| `summary_cap` | 4 KiB | the bound on `result.summary` (§7.4); a summary is a sentence for an operator, and the logs are where detail belongs |
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
(§7.3), and is deleted wholesale with `runtime/` on restart or takeover. The steady state is therefore
**four files**, rewritten 11,520 times a day, and a lost heartbeat costs nothing because §13.2's
staleness rule falls back to `launch.written_at`.

Revision 5 said heartbeats are "never fsynced". They are: `atomic_write_json` writes through
`atomic_write_text`, which fsyncs the temporary file's descriptor and then the parent directory before
returning [C9], and a heartbeat written any other way would be a second write primitive to maintain.
What is true — and what that sentence was reaching for — is that **nothing treats a heartbeat as an
authority**, and `runtime/` is deleted wholesale rather than reconciled, so its durability buys nothing
and costs nothing that matters. The saving is in the file count and the retention policy, not in the
barrier: 4 files instead of 11,520, none of them validated on a projection, none of them retained
(§7.7).

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
reads `plans/<task-id>.<plan-hash>.json` for its `instruction`, checks, claims and `cwd`, and
`prepared.json` for the baseline digests. It receives no project state, no task list, and no
authorization decision — those were made before it started, and a worker that could re-read them could
also disagree with them.

### 16.2 What a worker may write

- Its own outputs: every path in the plan's `writes[]`, and nothing else. `store:<attempt-id>` is
  implicitly granted (§9.2), so the worker writing its own records is legal.
- `captures/<check-id>/<NNNN>-<capture-id>.json`, one per check execution. The capture has no assessment
  fields at all (§11.2), so there is nothing for the worker to leave null.
- `scopes/check:<check-id>#<NNNN>.json`, before running a check the plan assigns it (`executor: worker`),
  because a scope record is what authorises the write (§6.1).
- `logs/<check-id>/<NNNN>.out` and `.err`, truncated to `log_cap`.
- `result.json`, once, last, after fsyncing every declared output and its parent directory (O5).
- `captures/_conflict/<NNNN>-<capture-id>.json` when publish-if-absent returns `conflict` (§7.2),
  because a worker may not write a coordinator-owned `hold`.
- `runtime/heartbeat/<attempt-id>.json`, overwritten, if the adapter supports it.

It may not write `project.json`, `evidence.md`, `spec.md`, any other project file, any other attempt's
directory, the registry, or any coordinator-owned record kind (`reservation`, `prepared`, `launch`,
`sealed`, `classification`, `assessment`, `hold`, `resolution`, `stop-evidence`, `acceptance`, `fence`,
`commit-observed`, `generation-fence`, `release`, `owner`, `stop-request`, `stop-clearance`). This is the
existing canonical-state ownership rule [C5] restated at the level of record kinds.

### 16.3 What a worker must do

1. Verify that `prepared.json` exists and that its `attempt_id` matches. If not, write nothing and
   exit non-zero: it was started for an attempt that does not exist.
2. Re-digest every read claim and compare to the baseline. A mismatch is not the worker's to resolve:
   it records the mismatch in `result.json` and does not run the checks. The coordinator raises
   `stale_evidence`.
3. **Perform the task**: carry out the plan's `instruction` (§9.1), writing only paths in the plan's
   `writes[]`. This step is the reason a worker exists, and revision 5 omitted it — its five steps went
   from checking the baseline straight to running the checks, so the contract described a verifier and
   the document called it an executor. Nothing else in the protocol says what a worker does, and a
   worker cannot infer the work from the checks: `pytest -q` passing is not a description of the change
   that makes it pass.
4. Run each check in the plan's order whose `executor` is `worker`, from the plan's `cwd`, capturing per
   §11.2, publishing that execution's `scope` record first. It records the exit variant it observed; it
   never converts `signalled` into a code, and never invents `unknown` for a status it did have.
5. Digest every declared write claim and every subject, before and after each check.
6. Publish `result.json` with exactly the fields §7.4 names — `outcome`, `produced`, `baseline_matched`,
   `captures`, `summary`, and `refusal_code` when refused — and no others. Revision 5 described this
   record in three places with three different field lists (`checks_run` here, a capture-reference tuple
   in §7.4, a `produced` map in §9.4); §7.4 is now the only definition, `captures` holds
   `{check_id, sequence, capture_id}` references, and `checks_run` is deleted as a count derivable from
   that list.

### 16.4 What a worker must not do

- **It does not assess.** It publishes no `assessment` record and writes no judgement in any form
  (§11.5); adequacy is the coordinator's, on evidence the worker produced.
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

- The producer is the coordinator, so it may not assess its own `criteria` checks. In v1 that is caught
  at admission, not at acceptance: a plan whose `criteria` check has no executor but the coordinator is
  refused `R-SELF-ASSESSMENT` and the task quiesces to the v3 sequential path (§11.5). An inline
  attempt therefore runs only `command` checks, whose assessor is `exit_status` and needs no producer
  distinction.
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

Claude Code and Codex are both hosted as non-interactive POSIX subprocesses. They share the same
process-tree lifecycle adapter; host-specific command construction is limited to safe allowlisted
arguments and each CLI's non-interactive isolation flags.

| Host | Version | `start` | `observe` | `seal` | `terminate` | Consequence |
|---|---|---|---|---|---|---|
| Claude Code CLI | discovered by `--version` | `Popen` in a new session | `waitpid` / `kill(pid, 0)` | process group reaped and exit attested | `killpg` | supported in an isolated Git worktree |
| Codex CLI | discovered by `--version` | `Popen` in a new session | `waitpid` / `kill(pid, 0)` | process group reaped and exit attested | `killpg` | supported in an isolated Git worktree |
| Local subprocess adapter | POSIX | `Popen` in a new session | `waitpid` / `kill(pid, 0)` | process group reaped and exit attested | `killpg` | shared lifecycle implementation |

The sealable unit is the short-lived CLI process tree, not a resumable in-process agent session. The
adapter retains the `Popen` object until observation and sealing are complete, starts a new process
group, and attests the exit code only after reaping it. This is why both hosts can satisfy I2 without
claiming that a host-native resumable conversation has terminated forever. If neither executable is
available, admission returns `R-NO-ADAPTER` before dispatch and the coordinator uses the sequential
path.

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
   [C6]. An undeclared output is invisible to the conflict relation (§9.3) and to
   reconciliation (§13.3). An output that is `required: true` and covered by no check refuses
   admission (`R-CHECK-UNCOVERED`).
2. **Write `verification` as commands, one per line**, with no `&&`, `||`, pipelines, redirection or
   unquoted `;` (§9.1). A composed command is not enumerable, so the task runs inline
   (`R-UNENUMERABLE`). Two lines run in parallel-eligible form; one line joined by `&&` does not.
3. **Name files, not directories**, as subjects and outputs. A directory subject refuses
   (`R-DIRECTORY-SUBJECT`) because §9.2 has no digest for one and §22 defers the semantics.
4. **Keep the effect kind confined**, to `none` or `local_write`. Anything else is not admitted
   (`R-EFFECT-NOT-CONFINED`); a task with a `destructive` or `external` effect is a task this protocol
   does not execute at all, and it stays on the sequential path where its authorization already lives
   [C11]. There is no `effect.confined_to` to write: revisions 1 to 5 asked for one, and the shipped
   validator accepts only `kind` and `description` in an effect object, so a plan carrying it is
   rejected before any of this is reached [C32]. The write set is declared where the conflict relation
   reads it — in the plan's claims (§9.1) — and nowhere else.
5. **Split by resource, not by phase.** Two tasks that write the same file conflict (§9.3) however
   independent their descriptions are. Two tasks that read the same file do not. The largest
   practical win is from separating write sets, which is a property of the plan, not of the scheduler.

And the honest counterweight: of the 331 dependency levels §3.2 measured across 34 real graphs, 202
hold exactly one task. Following every rule above, most projects will still run mostly serially, because
most of their tasks depend on each other. The rules buy correctness at the boundary and a modest win
where width exists; they do not create width.

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

## 20. Implementation phases

Six phases, each separated from the next by a **review gate**, originally sequenced the implementation.
The 0.8.1 release candidate contains all six phases. The controlled benchmark and fresh real-project
pilot passed on 2026-09-16; the table remains as implementation provenance and as the acceptance
contract future changes must preserve.

| Phase | Deliverable | What stays disabled | Exit gate | Behaviour change |
|---|---|---|---|---|
| 1 | The claim representation and the conflict relation (§9.3) as a pure library, with its tests | everything else: no store, no locks, no CLI, no caller | the API and the conflict matrix reviewed against §9.3 | none |
| 2 | A one-host feasibility spike on a disposable project: can a separate agent be given a task, observed, and its scopes sealed (§16, §17)? | real projects, dispatch, any framework built on the answer | a real agent task demonstrated, with defensible lifetime and stop behaviour, or the boundary revised | none |
| 3 | The record schemas (§7.4), canonical serialization, digests (§11.2) and `publish_if_absent` (§7.2), with crash injection between durable effects | canonical mutation, task execution | identities, the immutable `assessment` record (§11.5), and real filesystem failure prefixes validated | none |
| 4 | The projection (§6.1, §6.3), the derived label (§6.2), the operations (§8) and recovery (§13) behind one guarded entry point, against a fake adapter and a test store | user-facing activation | successful, failed, stopped and interrupted runs all obeying the same operations, invariants asserted at the durable effects and not only at intent | none |
| 5 | `enable-execution` (§7.6), the v4 field, the reader-only refusal, and sequential integration at capacity one on a disposable project | concurrency | canonical commit, evidence, ownership and old-reader behaviour verified on every affected host | **yes** — a v4 project is unreadable by an installation without this phase, and activation is irreversible for the generation |
| 6 | An opt-in pilot at `max_concurrent` 2, plus the measurement harness of §3.2 and §21.3 | unsupported task shapes; unattended retry of an ambiguous start | real overlap shown safe, useful and measured against a sequential baseline | **yes** — concurrency |

Phase 1's original implementation contract — module path, public surface, refusal validation,
conflict matrix, tests, and non-goals — remains in
[`docs/parallel-execution-implementation-plan.md`](../../../../../docs/parallel-execution-implementation-plan.md).

Two phases change behaviour, and the earlier one is the one that matters. Phase 5 writes a schema field
older installations must refuse, so it is behaviour-changing for every host that has not been upgraded,
and the migration story belongs to it rather than to the phase that first executes a task. Phase 6 is
explicitly conditional: if its measurement on real graphs shows no win, the correct outcome is to stop
at Phase 5 with a protocol that runs sequentially, correctly, and refuses visibly — which is a better
result than concurrency nobody measured.

Phase 2 sat there deliberately because host lifetime was the first empirical uncertainty. The shipped
implementation resolves it by sealing non-interactive CLI subprocess trees rather than trying to seal
resumable host-native agent sessions (§17.1).

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
  by this revision — so shipping a revision without dispositioning the review it responds to fails;
- whatever §7.3's `content_digest` removes beyond what `body_digest` removes is a field of §7.4's
  **common** envelope and of no other table, and §7.2's `EEXIST` branch compares that digest and not
  bytes. This is the relationship, not the phrase: a field only the attempt envelope carries is part
  of what two publications must agree about, so excluding it would make two attempts' records compare
  equal, while a writer-stamped field left inside it makes every completion after a takeover a
  `conflict` (§8.2). Revision 6 shipped the byte comparison and this check is what would have caught it;
- every top-level family of §7.1's store layout is either fenced by §12.4, exempted by §7.5's D2 class
  or by §7.4's list of what is not a journal record, or is the generation fence itself — walked out of
  §7.1's tree rather than listed here, so adding a store path without fencing it fails. Revision 6
  fenced only `attempts/`, which left a durable stop request unexplainable after a takeover;
- §20's phase ids are contiguous from 1, the implementation plan carries one section per phase and
  no section for a phase §20 does not have, and each document links the other.

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
launch capability. Canonical state and external execution are modelled **separately**: a `Store` holds
the durable records, the grant registry and the canonical task state; a `World` holds the modelled
processes and the modelled bytes they write; no guard reads a process, and no invariant reads a label.
Crashes are injected between durable effects, at every prefix of every operation's sequence.

**One entry point.** A single `perform` runs an operation's guard, refuses without touching the store if
the guard refuses, and otherwise applies the durable effects one at a time, checking every invariant
after each. Recovery is a function from a world to a sequence of operations that `perform` then runs, so
a recovery step whose precondition has gone stale is refused rather than applied — which is the property
revision 5's model could not have, because it checked guards in one place and applied effects in
another. Guard bypass exists as an explicitly separate negative-test API used only by the class that
demonstrates each invariant is breakable.

Its scope is narrower than §8.1's table, and the difference is the point. **Modelled:** O1, O2, O3, O4,
O5, O6, O7, O8, O9, O10, O11, O12, O13, O16, O17 and O18. **Partly modelled:** O15, as far as its
durable stop request and its `operator_stop` holds — steps 3 to 5 ask an adapter to terminate something,
and there is nothing here to terminate. **Not modelled:** O14, whose durable shape is O12's over a
`BLOCKED` task, and O19 and O20, which need a second live coordinator process to be worth writing.

The properties it asserts are the five invariants, stated externally:

- **I1** no two executors ever hold conflicting claims, read off the grant registry — a structure
  separate from the journal, because O16 removes the grant in a *second* durable step after publishing
  the release, and a model that derives one from the other cannot express that prefix at all;
- **I2** no claim is released while any modelled process can still write: a process whose coordinator is
  dead and whose capability is unconsumed, and a worker that has published a result on a host whose
  adapter cannot seal it;
- **I3** the bytes and the definition a canonical mutation was computed over still hold **at the commit
  effect**. Revision 5 checked this at the intent, which is the one place it cannot fail — everything
  the mutation is exposed to happens between the intent and the commit;
- **I4** a task is `DONE` only if its `evidence` holds a receipt that recomputes from a published
  intent, checked both against the model's own receipt derivation and by building the real candidate and
  dry-running it through the real validator;
- **I5** no projection reads a malformed, unexplainable or **null** record as absence, and no operation
  reads a store holding an indeterminate record at all — a stored JSON null is a record that exists and
  cannot be read, not a missing file.

Each invariant is also shown breakable with the guard bypassed, because an invariant no scenario can
violate is not being checked.

Two defects in this document were found by writing the model rather than by reviewing the prose, and
both are amended above. §7.2 compared bytes to decide `identical` while §7.4's envelope carries the
writer's run, generation and timestamp, so no completion after a takeover could have been `identical`;
that is now `content_digest` (§7.3). And §12.4's generation fences were per-attempt only, so no fence
could ever explain `control/stop-requests/`, and a stop in force when a coordinator died would have read
as `indeterminate` for the rest of the project's life; O17 now publishes a store-root fence too. Each
has a named regression case.

Ten of review 04's eighteen findings are traces of this state machine, and each has a named regression
case: R4-01, R4-02, R4-03, R4-04, R4-06, R4-07, R4-08, R4-10, R4-12 and R4-14. The other eight are not
state-machine traces and are deliberately not modelled here — R4-05 and R4-11 are answered by §6 and
§11.1, R4-09 and R4-13 by refusals in §4, R4-15 by this replacement itself, R4-16 by §17.1, and R4-17
and R4-18 by the documentation checker and §21.3.

Review 05's R5-07 is answered by five repaired probes, each with a named regression case: a stored null
read as absence, a released claim on an unsealed worker, a recovery that committed without
revalidating, a capability consumed by a worker-written record, and a dispatch gate filtered by task
when §14.2 makes it project-wide.

The suite is deliberately far smaller than revision 4's: 46 tests and 56 subtests here against 70 tests
and 1,208 subtests there. That suite asserted label names, and its `apply_outcome` returned a constant
regardless of the crash prefix it was given, so most of those subtests could not fail.
**Passing this model does not establish that the protocol is correct.** It remains a design-stage
model rather than the shipped executor, filesystem store, and adapter. The real validator it calls
checks a candidate's schema and transitions rather than a receipt's meaning, so the receipt half of I4
is the model's own assertion; implementation tests supply a separate layer of evidence.

### 21.3 The benchmark

The implementation suite includes a real two-worker overlap fixture, isolated commits, serial
integration, check reruns, and failure cases. A controlled one-second-task benchmark measured 3.865s
at capacity one and 2.466s at capacity two: 1.567x faster with timestamps proving overlap. A fresh
schema-v4 pilot then launched Claude Code 2.1.273 and Codex CLI 0.154.0 together; both produced useful,
separately committed outputs, all checks passed after serial integration, and the target remained
clean. The pilot exposed and fixed one fresh-store defect before dispatch: same-volume checking now
walks to the nearest existing ancestor when `execution/runtime/tmp` does not yet exist.

These results verify overlap and the end-to-end lifecycle on one machine. They do not establish a
general performance factor: model latency, task duration, conflicts, and integration cost determine
whether a real project benefits.

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
| A second independent assessor, so a `criteria` check can be assessed where the coordinator is the only executor | `R-SELF-ASSESSMENT` | a second process with its own identity, which is a second executor and therefore its own I1 problem |
| Execution on a host that cannot keep a coordinator process alive across an attempt | `R-NO-RUNNER` | either a resumable coordinator whose whole state is the store, or a host-side runner, both of which change §12 rather than extend it |
| Ageing, priority, or any bound on how long a ready task waits | nothing: §10.2 promises determinism, not fairness, and a starved task is not an error | a queueing discipline and a measured reason to prefer it, neither of which this sample can supply |
| Alternate transports and stores (a database, a queue, a remote store) | the envelope of §3.1 | a durability and ordering argument for the replacement, not a port |
| Legacy-writer coexistence beyond an operator attestation | `R-LEGACY-WRITER` | a version negotiation older installations participate in, which they cannot, being older |
| Scheduling heuristics beyond plan order | §10.2 states plan order as the only tie-break | a measured win over plan order, which §3.2's widths suggest is unmeasurable at this scale |
| Inline execution of a task shape the protocol does not support | `R-UNENUMERABLE`, `R-DIRECTORY-SUBJECT`, `R-EXTERNAL-REFERENCE`, `R-EFFECT-NOT-CONFINED`, each of which now quiesces and hands the task back to the sequential path (§4) | claims for shapes the plan cannot enumerate, which is the same problem as the row above it |

## 23. Citations

Every row is re-read by the documentation test: the file is opened, the line range is sliced, and the
needle must appear inside it. A row whose needle has moved fails the test rather than aging quietly.
Each row is also required to be *used* somewhere in this document, and every `[Cn]` use is required to
resolve to a row — so a citation cannot be added and forgotten, or cited and never defined.

Read §21.1 before treating a row as verification of anything: it establishes that the reference exists,
not that the sentence citing it is true.

| Id | Source | Lines | Needle |
|---|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1335-1343 | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3081-3101 | `def _dependency_levels(` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3102-3130 | `def build_task_graph(` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3015-3022 | `def levels(` |
| C5 | `plugins/research/skills/project/SKILL.md` | 35-39 | `One coordinator owns canonical state` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md` | 9-14 | `One coordinator is the sole writer` |
| C7 | `plugins/research/skills/project/SKILL.md` | 143-149 | `Stay sequential unless parallel work is requested/authorized` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py` | 319-352 | `class DirectoryLock` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py` | 296-312 | `def atomic_write_text(` |
| C10 | `plugins/research/skills/project/scripts/workspace_lib.py` | 26-30 | `EFFECT_KINDS = {` |
| C11 | `plugins/research/skills/project/scripts/workspace_lib.py` | 33-37 | `REFERENCE_ROOTS = {` |
| C12 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2578-2586 | `unsupported schema_version` |
| C13 | `plugins/research/skills/project/scripts/workspace_lib.py` | 454-471 | `def _validate_evidence_reference(` |
| C14 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2875-2885 | `shell=False,` |
| C15 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2883-2891 | `command timed out after` |
| C16 | `plugins/research/skills/project/scripts/workspace_lib.py` | 2915-2928 | `must not see a half-written one` |
| C17 | `plugins/research/skills/project/scripts/workspace_lib.py` | 26-30 | `AUTHORIZATION_STATUSES = {` |
| C18 | `plugins/research/skills/project/scripts/workspace_lib.py` | 528-536 | `non-required authorization must use status` |
| C19 | `plugins/research/skills/project/scripts/workspace_lib.py` | 541-548 | `task requires explicit authorization` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py` | 59-65 | `TASK_TRANSITIONS = {` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3457-3465 | `IMMUTABLE_PROJECT_FIELDS = (` |
| C22 | `plugins/research/skills/project/scripts/workspace_lib.py` | 24-30 | `TASK_STATUSES = {` |
| C23 | `plugins/research/skills/project/scripts/workspace_lib.py` | 538-545 | `source and authorized_at must be null unless status is explicit` |
| C24 | `plugins/research/skills/project/scripts/workspace_lib.py` | 320-326 | `def __init__(self, path: Path, timeout: float = 5.0) -> None:` |
| C25 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3549-3557 | `lock_timeout: float = 5.0,` |
| C26 | `plugins/research/skills/project/scripts/workspace_lib.py` | 417-435 | `required must be a boolean` |
| C27 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1328-1336 | `BLOCKED task requires block_reason` |
| C28 | `plugins/research/skills/project/scripts/workspace_lib.py` | 82-96 | `TASK_FIELDS = {` |
| C29 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3535-3573 | `with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):` |
| C30 | `plugins/research/skills/project/scripts/workspace_lib.py` | 3586-3594 | `The commit already landed` |
| C31 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1276-1284 | `"success_criteria", "verification"` |
| C32 | `plugins/research/skills/project/scripts/workspace_lib.py` | 494-500 | `_unexpected_fields(value, {"kind", "description"}, label, report)` |

Eight rows are new in revision 5, and each exists because revision 4 asserted the constant without one:
C22 (`TASK_STATUSES`), C23 (the withdrawal rule that `source` and `authorized_at` must both go null),
C26 (`required` is a boolean on every output object), C27 (`block_reason` iff `BLOCKED`), C28
(`TASK_FIELDS`, which has no `notes` key), C29 (the lock the commit path holds across load, validate
and write), C30 (the index rebuild outside it), and C31 (`verification` as a required non-empty
string). C24 and C25 replace revision 4's stated lock timeouts with the code's actual 5.0-second
defaults.
