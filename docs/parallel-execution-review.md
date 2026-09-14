# Review of the parallel execution design

Date: 2026-09-11

Reviewed branch: `parallel-execution-design`

Reviewed commit: `e18e589423a7116781c1aaf304aa927d421b8a49`

Design: [parallel-execution.md at the reviewed commit][design]

This review covers the design and the existing task-state, evidence, and validation implementation.
The parallel executor is not implemented in the reviewed commit. Findings below describe problems
in the proposed protocol, not bugs in a shipped executor.

The shared checkout changed to `memory-index-postmortem-cap` during the review. Source inspection
and final verification were completed against an isolated snapshot of the reviewed commit. This
document records feedback; it does not implement the proposed changes.

## Assessment

Revise the design before implementing it. The single coordinator, dependency-based scheduling, and
bounded worker context are sensible foundations. The weak points are crash recovery, ownership of
shared files, and the amount of infrastructure introduced before the execution protocol is fully
specified.

Preserve these decisions:

- `project.json` remains authoritative for task state.
- One coordinator owns canonical state and shared evidence.
- Scheduling follows satisfied dependencies rather than level barriers.
- Workers receive bounded task context and return concise, structured results.
- Concurrency is bounded, and sequential execution remains supported.
- Parallelism is used where the work benefits, without artificially fragmenting coherent tasks.

The recommended first implementation is coordinator-assigned execution attempts, a durable result
inbox, and one ready-queue scheduler. SQLite and MCP can be added behind that protocol if they solve
a demonstrated need.

## Findings

### 1. High priority: the queue is not reconstructible as claimed, and draining can lose results

Design references: §5.2, especially I2; §6.2, `queue_drain`.

I2 says deleting `queue.db` loses at most in-flight claims. However, the database also holds submitted
results that have not reached `project.json`, including timing and verification metadata. Canonical
task state cannot reconstruct those records.

More seriously, `queue_drain` marks results read in its transaction, before the coordinator commits
them:

```text
Worker submits success
    → coordinator drains and marks it read
    → coordinator crashes before committing
    → restart finds neither a pending result nor a completed task
```

The result row might still exist, but the specified recovery API no longer returns it. The proposed
schema also lacks a field representing whether a result was read or integrated.

Replace consumption on read with repeatable delivery and acknowledgement after canonical commit:

1. Reading pending results does not consume them.
2. Each execution attempt and result has a stable identifier.
3. The coordinator records the accepted result identifier in canonical evidence.
4. Only after the canonical commit does it acknowledge integration.
5. On restart, an already committed result is acknowledged without executing or recording it again.

Classify the stores explicitly. Task mirrors are disposable; unintegrated execution records are
durable. Either preserve those records outside SQLite or give the database retention and recovery
rules. A store need not be authoritative for task status to contain irreplaceable execution evidence.

### 2. High priority: lease expiry can create two writers for the same output

Design references: I5; §7.2; §9.

The protocol immediately makes an expired task claimable again. Expiry does not establish that its
worker stopped:

```text
Worker A starts a long command
    → heartbeat deadline passes
    → worker B reclaims the task
    → A and B both write the same files
```

Rejecting A's eventual submission protects the database, but cannot undo its filesystem writes. An
agent can also miss a heartbeat while legitimately waiting on a long tool call.

Introduce an `attempt_id` and generation number. Require them on every heartbeat and submission.
These identifiers reject stale protocol messages; they do not isolate filesystem writes.

For the proposed shared working tree, use this default:

1. Expiry changes the attempt to `UNCERTAIN`.
2. Its output reservations remain held.
3. The coordinator establishes that the old execution stopped and inspects partial outputs.
4. A replacement attempt starts only after reconciliation.

Automatic retry is appropriate when the old attempt's writes are confined to a disposable location
whose outputs cannot be accepted after revocation.

Reservations must also survive worker submission until verification and integration finish.
Otherwise, a subsequent task can modify an artifact while the coordinator is checking it.

### 3. High priority: the start protocol and stale-plan behavior are unspecified

Design references: §6.2; §7.1; §7.4; §9.

The existing skill requires a task to be committed as `RUNNING` before its action. The proposed
worker can claim and execute, while the coordinator's documented loop concentrates on integrating
completed results. There is no explicit handoff that satisfies the existing rule.

Define this order:

```text
Reserve attempt and resources
    → commit task RUNNING
    → release that attempt to execute
    → accept its result
    → verify and commit DONE/BLOCKED
```

Specify recovery between every pair of steps, especially a crash after committing `RUNNING` but
before spawning the worker.

Eligibility needs more than “dependencies are `DONE`.” It must check:

- The task's own status.
- Authorization requirements, including local tasks that explicitly require authorization.
- Whether another attempt is active or awaiting reconciliation.
- Whether the project permits execution and dispatch is paused.
- Resource reservations and available host capacity.

Reseeding during execution also needs defined semantics. If output paths, verification, or success
criteria change while a worker runs, which definition governs its result? A project revision alone
is insufficient: unrelated sibling completions legitimately increment it.

Give each attempt an immutable execution contract hash covering its relevant task definition and
input references. Freeze that contract while running, or explicitly invalidate and reconcile the
attempt. Accept results only against the contract they executed. Do not invalidate an attempt merely
because an unrelated task advanced the project revision.

### 4. High priority: disjoint declared outputs are insufficient isolation

Design references: §7.1; §9; §13, U1 and U2.

The design conflates deliverables with resource ownership. Two tasks can write different output
files while conflicting through:

- One task reading files another is changing.
- Shared `.coverage`, build, cache, or temporary files.
- Git's index, checkout, or branch state.
- A directory declaration overlapping another task's child file.
- Different rooted references resolving to the same physical location.

The coordinator's verification commands and local actions participate in these conflicts too.
Separate project databases do not prevent two projects from modifying the same target directory.

There is also a scheduling issue: requiring outputs to be disjoint from every claimable task can
exclude both members of a conflicting pair. Usually one should run and the other should wait.

Use a deterministic resource-selection rule:

```text
Choose ready tasks in priority order.
Admit a task if its resource claims do not conflict
with active attempts or tasks selected in this dispatch.
```

Resolve rooted paths before comparison; treat directory ancestry as overlap; protect canonical files
explicitly. Account for reads when inputs are mutable. For repository-wide checks or shared Git
operations, a coarse exclusive resource is an efficient first implementation.

A post-task path comparison provides useful diagnostics, but does not reliably identify which
concurrent worker changed a file or detect transient writes later reverted. Do not present it as
complete enforcement of output ownership.

### 5. High priority: SQL predicates do not establish worker identity

Design references: I3; §6.2; §9.

The design claims workers cannot change another worker's rows, but `worker` is supplied by the
caller. A predicate such as `WHERE worker = ?` checks a string, not the caller's identity.

Parameterized SQL prevents injection; it does not grant authority.

Bind operations to a server-issued attempt capability or authenticated session. Derive task and
worker associations server-side. Reject an attempt token used for a different project or operation.

Keep the threat model realistic: a worker with unrestricted shell access to the same user-owned
files can potentially bypass the API entirely. Tool restrictions and tokens can prevent accidental
misuse within the protocol; they are not filesystem isolation.

This also applies to the claim that workers have no tool capable of running `commit`. A shell that
can edit outputs can generally invoke the launcher unless the host actually restricts it.

### 6. High priority: Class B evidence weakens a guarantee the repository deliberately added

Design reference: §10.

The design acknowledges that a fabricated passing transcript would be accepted, but argues this is
sound because the worker is already trusted to write outputs. Those are different assurances.
Permission to produce a deliverable does not establish that verification executed. Checking whether
a transcript says “success” proves only that the transcript says so.

Separate capturing execution from appending canonical evidence:

1. A helper runs the actual command and captures its exit status and output.
2. It produces a structured receipt associated with the attempt and artifact identities.
3. The coordinator imports that receipt into evidence without rerunning the command.
4. Evidence identifies whether verification was coordinator-executed, runner-captured, or merely
   worker-reported.

This still relies on the execution environment's trust boundary, but avoids asking the model to
author its own process result. A receipt stored in a worker-writable location is not automatically
tamper-proof; document that limitation rather than implying otherwise.

Rerunning remains useful for cheap independent checks. It must occur against stable inputs: a
coordinator rerun while siblings modify the tree may check a different state from the one the worker
produced.

The current `verification` field permits inspection or review descriptions, not just executable
commands. The execution contract needs to distinguish these methods rather than treating every
verification string as executable shell text.

### 7. Medium priority: the lock argument does not justify the MCP architecture

Design references: §4; §14.

The design ties project-lock contention to index rebuilding. The implementation releases
`.project.lock` before rebuilding the index. Evidence commands likewise execute before acquiring
that lock; the lock protects the append. See the reviewed implementations of
[`commit_candidate`][commit-code] and [`record_evidence`][evidence-code].

Contention remains possible, but the document does not establish its duration or frequency. It also
proposes coordinator-only evidence recording, which already removes worker contention on that path.

A durable handoff mechanism is justified by recovery. A worker-pulled SQLite queue exposed through a
custom MCP server needs a separate justification.

At four workers, start with coordinator assignment and durable per-attempt results. The coordinator
already owns the decisions needed before and after execution. Workers do not need to discover or
claim their next task themselves.

### 8. Medium priority: transport and runtime compatibility need concrete gates

Design references: §5.2; §6; §7.3; §7.4; §15.

The host gate is promised but not specified as an operational contract. Define required support for
spawning, bounded context, result delivery, worker status, interruption behavior, available capacity,
and any tool restrictions the design relies on.

Four should be a default ceiling, not a required worker count. The review session exposed four total
agent slots including the coordinator, leaving three worker slots.

MCP needs an explicit protocol-version target. The document's `initialize` model corresponds to
older versions; the published 2026-07-28 stdio specification uses per-request metadata and documents
compatibility probing for legacy initialization. A handwritten adapter needs deliberate version
coverage. See the official [2025-11-25 lifecycle][mcp-legacy] and
[2026-07-28 stdio specification][mcp-stdio].

There is a concrete SQLite issue too. The design reports SQLite **3.51.0**. SQLite documents a rare
WAL concurrency corruption bug affecting that version, fixed in 3.51.3 and selected backports. Probe
the actual runtime and choose a patched version, rollback journaling, or a connection architecture
that avoids the affected concurrent access. WAL availability alone is insufficient validation.
See [SQLite's WAL-reset bug documentation][sqlite-wal-bug].

### 9. Medium priority: performance conclusions exceed the measurement

Design references: §2; §7.3; §7.4; §8.

The 31-project sample usefully discourages extravagant speedup claims. It does not establish that
four workers are universally optimal, nor does it measure the stated context-economy benefit.

Dependency-level width is structural. Actual scheduling depends on task durations, conflicts,
verification, integration cost, and host capacity. Commit the benchmark script and reproducible
graph fixtures alongside the figures.

Change “plans authored wider” from an objective into a planning technique. The objective should be
lower completion time or coordinator context consumption while preserving quality and controlling
total cost. Artificially wider plans can improve the metric while increasing assembly and review
work.

Batching deserves a less absolute treatment. Waiting briefly to commit several completed results
does not falsify history, provided actual completion times are preserved and dependent tasks remain
gated on canonical completion. The text's move from “about `N + 1`” toward “one per task” also does
not explain an increased commit count.

## Recommended execution protocol

Implement coordinator-assigned attempts, a durable result inbox, and one ready-queue scheduler.
Sequential execution uses the same scheduler with capacity one. A separate wave scheduler is not
necessary merely to support hosts without workers.

### Attempt lifecycle

```text
PREPARED
   │ canonical task committed RUNNING
   ▼
DISPATCHED → RUNNING → RESULT_READY → VERIFIED → INTEGRATED
                │
                └─ lost contact → UNCERTAIN → reconcile before retry
```

These are execution-attempt states. They do not replace canonical task statuses. Failed and blocked
results also remain available for integration; they must not disappear because they cannot follow
the successful verification path.

### Persistent records

A minimal layout could be:

```text
<project-dir>/execution/
    attempts/<attempt-id>/
        dispatch.json
        result.json
        verification.json
        stdout.log
        stderr.log
    receipts/<content-hash>.json
    runtime.json
```

The coordinator owns dispatch records and runtime bookkeeping. A worker or capture helper writes
only its assigned result location. Final receipts are immutable, validated, and referenced from
canonical task evidence. Temporary result files become visible through atomic replacement after
writing completes.

Existing rooted evidence references can identify accepted receipts. Timing and attempt metadata can
initially live in those receipts, so splitting `TASK_FIELDS` and adding timing fields need not be
the first implementation dependency. Readers and reports must explicitly support these receipts;
the current task graph will not discover their timing automatically.

| Record | Essential contents |
|---|---|
| Dispatch | Project/task/attempt IDs, coordinator generation, contract hash, input identities, allowed resources, verification definition |
| Result | Attempt ID, contract hash, outcome, artifact references and hashes, concise handoff, verification receipt references |
| Verification | Actual argument vector or review method, working directory, start/end times, exit status, output references, provenance class |
| Integration receipt | Attempt/result identity, accepted artifact and verification identities |
| Runtime bookkeeping | Active attempts, reservations, dispatch pause, host handles, reconciliation status |

Record dispatch, actual start, finish, and integration separately where observable. Use a monotonic
clock for elapsed durations during execution and UTC timestamps for reporting. A task with retries
needs multiple attempt records, not one overwritten start/end pair.

If task schema fields are added later, define reader/writer compatibility explicitly. Making fields
optional in a new reader does not make new records readable by old installations that reject unknown
fields. Do not backfill timing into immutable terminal tasks.

### Coordinator loop

1. Load and validate canonical state; reconcile outstanding attempts before starting new work.
2. Inspect pending results without consuming them.
3. Validate attempt identity, execution contract, artifacts, and verification.
4. Write an immutable integration receipt and append evidence idempotently.
5. Commit the task transition with a reference to that receipt.
6. Mark the result integrated and release its resource reservations.
7. Select newly ready tasks that fit capacity and resource constraints.
8. Prepare their attempts, commit them `RUNNING`, then dispatch.

Use a single-coordinator ownership guard separate from `.project.lock`. Takeover must reconcile
existing attempts; a revision conflict alone does not prevent two coordinators from dispatching
duplicate physical work. Do not hold the project lock throughout agent execution.

### Recovery contract

| Interruption point | Required recovery |
|---|---|
| Attempt prepared, task not committed `RUNNING` | No execution permitted; reconcile or discard preparation |
| Task `RUNNING`, worker not launched | Reconcile dispatch; do not invent a completed execution |
| Worker finished, coordinator has not read result | Durable result remains pending |
| Evidence appended, canonical commit absent | Retry integration without duplicating evidence |
| Canonical commit landed, acknowledgement absent | Recognize the exact receipt and acknowledge without rerunning |
| Canonical commit landed, index rebuild failed | Treat execution as committed; repair the index |
| Worker contact lost | Retain reservations until termination or isolation is established |
| Task contract changed | Quarantine the old result for reconciliation |

For failure handling, persist a dispatch pause before starting further work. Continue collecting
independent siblings where appropriate, but add a bounded shutdown/reconciliation policy. “Let every
live lease finish” can otherwise wait indefinitely.

Keep the project `EXECUTING` while siblings remain running; the current validator forbids a
`BLOCKED` project with running tasks. Move it to `BLOCKED` after the running work has been reconciled.

## Filesystem capabilities and rollout

Introduce filesystem behavior progressively:

| Mode | Suitable first use |
|---|---|
| Read-only workers with separate result files | Surveys, analysis, independent review |
| Shared target with explicit reservations | Cooperative workers producing bounded, disjoint files |
| Isolated staging directories | Plain-directory projects needing safer retries and controlled promotion |
| Git worktrees | Repository tasks benefiting from independent working copies and integration checks |

Worktrees should remain an option even though some projects are not repositories. They separate
working copies, but do not themselves prevent a worker from accessing other paths. Staging also
requires explicitly reconciling the current skill instruction to work in the actual target location.

Where two projects share a target, define target-level coordination or decline concurrent writes.
A separate queue per project provides no cross-project resource guarantee.

## Efficient implementation order

| Stage | Changes | Exit condition |
|---|---|---|
| 1. Correct the specification | Replace reconstructibility, drain, lease, identity, and lock claims; define attempt states and recovery | Every transition has a crash/retry outcome |
| 2. Build the execution core | Scheduling, resource conflicts, contract hashes, attempt validation | Deterministic tests pass without a host |
| 3. Add durable handoff | Atomic manifests, immutable receipts, idempotent evidence integration | Restart tests preserve pending and committed work |
| 4. Add capture and isolation | Reuse process capture, record provenance, protect artifact verification | Failed/stale verification cannot become accepted success |
| 5. Wire hosts | Small host-specific dispatch instructions/adapters and capability checks | Each host demonstrates parallel operation or sequential fallback |
| 6. Enable the skill | Worker contract, execution loop, reporting, aligned release versions | End-to-end scenarios pass on all affected host surfaces |
| 7. Reassess infrastructure | Benchmark context, duration, integration overhead, and cost | Add SQLite/MCP only for a demonstrated need |

Keep new code in focused modules such as `execution_lib.py`, `execution_store.py`, and a capture
helper rather than expanding `workspace_lib.py` substantially. Expose operations through existing
launcher conventions. If MCP becomes useful later, it should adapt those operations rather than own
scheduling or recovery semantics.

Preserve the repository's implementation constraints:

- Shipped scripts remain stdlib-only and compatible with Python 3.9.
- Public functions are typed; project state remains `dict[str, Any]`.
- Malformed JSON produces findings or `WorkspaceError`, not unexpected tracebacks.
- Canonical commits retain revision checks and the `project.json` commit point.
- Post-commit index rebuilding continues through `_rebuild_index_after_commit`.
- New tests live under `tests/plugins/research/project/`.
- Plugin release versions remain aligned across all three hosts and `uv.lock`.
- Skill activation follows implementation and host validation.

If SQLite is retained immediately, use attempt-based primary keys, explicit integration state, and
transactions that combine eligibility checks with reservation. Enable foreign-key enforcement on
every connection. Do not hold transactions during agent execution or verification. Specify backup
behavior for pending results: SQLite's WAL is persistent database state, so copying only `queue.db`
can lose committed transactions. See [SQLite foreign keys][sqlite-foreign-keys] and
[WAL persistence][sqlite-wal].

## Verification plan

The highest-value tests exercise failures and interleavings:

- Simultaneous reservations cannot exceed capacity or acquire conflicting resources.
- Expired attempts cannot submit as newer attempts or trigger overlapping shared-tree retries.
- Replaying a result is harmless; conflicting duplicate submissions are rejected.
- Every interruption in the recovery table can resume correctly.
- Sibling revision changes remain valid; changes to the executing task's contract do not.
- Directory, rooted-path alias, canonical-file, and verification conflicts are detected.
- A worker failure pauses dispatch while successful siblings remain recordable.
- Old project records and immutable terminal history remain unchanged.
- Each host executes a small independent pair, a dependency join, and a failure/restart scenario.

Use an injected clock and controlled process synchronization for timing and concurrency tests rather
than relying on long sleeps. Tests should check observable invariants, not merely mirror helper
implementations. Run whole-repository lint and type checks and the required coverage gate when the
implementation is ready.

Publish a reproducible benchmark covering both useful parallelism and overhead. Measure coordinator
context consumption separately from total model usage, and record worker startup, verification,
integration, retries, and end-to-end completion time. Compare capacity one with larger capacities
using the same task contracts and verification requirements.

## Review validation and limits

The isolated snapshot of `e18e589423a7116781c1aaf304aa927d421b8a49` passed **149 focused tests plus
53 subtests**, covering:

- `tests/plugins/research/project/test_record_evidence.py`
- `tests/plugins/research/project/test_review_regressions.py`
- `tests/plugins/research/project/test_workspace.py`

The tests were run with the existing virtual environment's Python, with coverage and pytest's cache
provider disabled. The initial `uv` invocation could not access its cache under the sandbox; invoking
the existing interpreter directly allowed the checks to complete.

These checks support the observations about existing evidence and state behavior. They do not
validate the proposed parallel executor, host integration, or the design's performance figures.
The external MCP and SQLite references were checked on the review date.

[design]: https://github.com/nampham2/agents/blob/e18e589423a7116781c1aaf304aa927d421b8a49/plugins/research/skills/project/references/parallel-execution.md
[commit-code]: https://github.com/nampham2/agents/blob/e18e589423a7116781c1aaf304aa927d421b8a49/plugins/research/skills/project/scripts/workspace_lib.py#L3193-L3230
[evidence-code]: https://github.com/nampham2/agents/blob/e18e589423a7116781c1aaf304aa927d421b8a49/plugins/research/skills/project/scripts/workspace_lib.py#L2478-L2601
[mcp-legacy]: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
[mcp-stdio]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
[sqlite-wal-bug]: https://www.sqlite.org/wal.html#walresetbug
[sqlite-foreign-keys]: https://www.sqlite.org/foreignkeys.html
[sqlite-wal]: https://www.sqlite.org/wal.html#the_wal_file
