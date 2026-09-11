# Parallel task execution

> **Status: design awaiting implementation.** Nothing in this repository implements this protocol.
> No script, schema field, MCP server, or `SKILL.md` step exists for it, and this document is not
> linked from `SKILL.md` precisely so that no coordinator is told to follow a protocol the tooling
> cannot execute. It is a specification for a successor project to build against, and a record of
> the decisions and measurements behind it. Until that project ships, `research:project` executes
> tasks sequentially and delegation is out of policy.

## 1. What this designs

A protocol for executing the tasks of one `research:project` plan **concurrently**: independent tasks
claimed by Claude Code subagents from a durable work queue, with one coordinator remaining the sole
writer of canonical state.

It is a protocol, not an engine. The machinery it needs mostly exists already; §3 says exactly what.

Two goals are co-equal, and the second is the less obvious one:

1. **Plans authored wider.** Concurrency cannot help a plan shaped like a chain, and §2 shows that
   this workspace's plans are shaped like chains. Guidance that changes how plans are written is
   therefore part of the deliverable, not a footnote to it (§8).
2. **Context economy.** A worker's tool output never enters the coordinator's context window. On a
   long project this is plausibly the larger benefit, and it is unrelated to wall-clock time.

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
charges nothing for spawning, prompt authoring, result integration, extra commits, or lock
contention. Loaded honestly, some of these projects would run *slower* in parallel.

Three consequences shape everything below.

- **Wall-clock speedup is not the justification.** A design sold on 1.45x, measured optimistically,
  against real overheads it does not count, would not survive contact with a real project.
- **A cap of 4 is free.** It is within 0.04x of unlimited across all 31 projects, and mean maximum
  level width is 3.87. §7.3 fixes the bound there rather than inviting a tuning exercise nobody has
  evidence for.
- **The bottleneck is the plan, not the executor.** This is why §8 exists.

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
  orders tasks by `(level, id)` [C3] while `TaskGraph.levels` counts distinct levels [C4]. A level is
  exactly a set of mutually independent tasks.
- **The ownership model is already normative.** One coordinator owns writes to `project.json`, shared
  records and `INDEX.md`; workers may write only assigned non-overlapping output paths and must not
  edit canonical state [C5], stated identically in the schema reference [C6]. The planner is already
  told to use a dependency graph only where independent work can run in parallel, to assign
  non-overlapping outputs, and to keep one canonical writer [C7].
- **Cross-process safety already exists.** A `mkdir`-based `DirectoryLock` [C8] plus
  `commit --expected-revision` gives optimistic concurrency: two writers means one loses, reloads,
  and reconciles.

So four things are missing, and they are what this document supplies: a **scheduler**, a **worker
contract**, a **result-and-evidence integration protocol**, and a **host-capability gate**.

## 4. Architecture

```
                    ┌────────────────────────────────────────────┐
   canonical  ──────│  project.json   (revision-checked commit)   │  coordinator writes only
                    └────────────────────────────────────────────┘
                                      ▲   copies timing, statuses, evidence
                                      │
                    ┌────────────────────────────────────────────┐
   derived    ──────│  queue.db  (SQLite, WAL)  <project-dir>/    │  coordinator + workers
                    └────────────────────────────────────────────┘
                          ▲ claim / heartbeat / submit
                          │  (JSON-RPC over stdio, MCP)
                    ┌─────┴──────┐  ┌────────────┐  ┌────────────┐
                    │  worker 1  │  │  worker 2  │  │  worker N  │   N ≤ 4
                    └────────────┘  └────────────┘  └────────────┘
                          shared working tree, disjoint declared outputs
```

Three layers, and the middle one is the whole idea: a **derived** store that workers may write,
sitting between canonical state that only the coordinator may write and workers that must be able to
coordinate without taking the project lock.

The reason the middle layer must exist is specific and measurable. `record_evidence` appends to
`evidence.md` under `.project.lock` — deliberately the same lock `commit` takes, so a commit never
sees a half-written evidence file [C9] — and `record-evidence` is the one mutating subcommand with no
`--lock-timeout` flag, pinned at the 5.0 s default [C8] (an absence has no line to cite; see §16). Meanwhile every commit regenerates both root
caches under a second lock, so a commit is not a short critical section. N workers contending on that
lock against a committing coordinator is the failure this architecture is shaped to avoid.

## 5. The queue

One database per project, at `<project-dir>/queue.db`, in WAL mode. Per project rather than per
workspace root: it matches how every existing lock and file is scoped, keeps concurrent projects from
contending at all, and keeps a project's scheduling state inside the directory that *is* its record.

### 5.1 Schema

```sql
CREATE TABLE task (                      -- derived mirror, seeded from project.json
    id                    TEXT PRIMARY KEY,
    level                 INTEGER NOT NULL,
    effect                TEXT    NOT NULL,      -- none | local_write | destructive | external
    delegable             INTEGER NOT NULL,      -- 0/1, computed per §7.1
    outputs_json          TEXT    NOT NULL,      -- declared outputs, for the disjointness check
    depends_on_json       TEXT    NOT NULL,
    status                TEXT    NOT NULL,      -- mirror of canonical status at seed time
    seeded_from_revision  INTEGER NOT NULL
);

CREATE TABLE claim (
    task_id           TEXT PRIMARY KEY REFERENCES task(id),
    worker            TEXT NOT NULL,
    claimed_at        TEXT NOT NULL,             -- RFC 3339, timezone-aware
    lease_expires_at  TEXT NOT NULL,
    heartbeat_at      TEXT NOT NULL
);

CREATE TABLE result (
    task_id       TEXT NOT NULL REFERENCES task(id),
    worker        TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    outcome       TEXT NOT NULL,                 -- done | failed | blocked
    verify_path   TEXT,                          -- declared output holding verification transcript
    verify_exit   INTEGER,                       -- exit code the worker observed
    notes         TEXT,
    PRIMARY KEY (task_id, worker)
);

CREATE TABLE event (                             -- append-only audit of every queue transition
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    task_id  TEXT,
    actor    TEXT NOT NULL,
    kind     TEXT NOT NULL,                      -- seed | claim | heartbeat | submit | expire | drain
    detail   TEXT
);
```

Timestamps are timezone-aware RFC 3339 strings, matching the rule canonical state already enforces on
authorization and receipt stamps.

### 5.2 The five invariants

**I1 — Canonical authority.** The queue never decides a task's status. It records claims and
returned results; the coordinator reads them, builds a candidate, and commits through the existing
revision check. `task.status` is a mirror and is never read as truth.

**I2 — Reconstructible.** `queue_seed` rebuilds `task` from `project.json` at a named revision.
Deleting `queue.db` loses at most the currently in-flight claims. The queue therefore carries no
backup obligation, needs no consistency relationship with `commit`, and cannot corrupt a project.

**I3 — Writer separation enforced in SQL.** A worker may write only its own `claim` row and a
`result` row for a task it holds an unexpired lease on. Enforced by the server's parameterized
statements — `INSERT INTO claim ... WHERE NOT EXISTS(...)`, and `UPDATE ... WHERE worker = ?` — not
by asking workers to behave. This is the one shared mutable thing a worker may touch, *precisely
because it is not canonical*, so the ownership rule [C5] is untouched.

**I4 — No shared lock with `commit`.** Queue traffic uses SQLite's own `busy_timeout` and never
acquires `.project.lock`. This retires the 5 s ceiling described in §4.

**I5 — Leases, not assignments.** A claim carries `lease_expires_at`. An expired claim is
reclaimable, so a dead or wedged worker cannot hold a task forever.

Verified available on the floor interpreter (`AGENTS.md` requires stdlib-only on stock macOS Python
3.9.6 [C11]): stock `/usr/bin/python3` 3.9.6 ships `sqlite3` against SQLite 3.51.0,
`PRAGMA journal_mode=wal` returns `wal`, and two connections opened with `timeout=5.0` serialize
correctly on `BEGIN IMMEDIATE` with both writes landing. No compiled extension and no FTS5 is
required, so the rejection recorded against SQLite FTS5 with `sqlite-vec` [C12] does not apply —
a distinction, not a reversal of it.

## 6. The MCP server

### 6.1 Transport

A **hand-rolled JSON-RPC 2.0 server over stdio**, stdlib-only, running on Python 3.9.6. The official
`mcp` SDK is not importable by the floor interpreter, so depending on it would put a bootstrap step
between a coordinator and its queue — and a failed bootstrap is discovered at the moment the queue is
needed, which is the exact objection on which a background daemon and a compiled search index were
already rejected [C12]. The protocol surface a queue needs is small: `initialize`, `tools/list`,
`tools/call`.

Cost, stated plainly: every line of it falls under the repository's 100% coverage gate [C13], and the
server is the first MCP component in a repository that has none — declared to three hosts through
three separate marketplace files, none of which carries MCP wiring today.

### 6.2 Tool surface

Coordinator tools:

| Tool | Arguments | Returns |
|---|---|---|
| `queue_seed` | `project_dir`, `revision` | rows written; rejects if `revision` ≠ current `project.json` revision |
| `queue_status` | `project_dir` | per-task status, live claims, lease deadlines |
| `queue_ready` | `project_dir`, `limit` | delegable tasks whose dependencies are `DONE` and whose outputs are disjoint from live claims |
| `queue_drain` | `project_dir` | unread `result` rows, marked read in the same transaction |
| `queue_expire_leases` | `project_dir`, `now` | claims whose lease has passed, released and logged to `event` |

Worker tools:

| Tool | Arguments | Returns |
|---|---|---|
| `queue_claim` | `project_dir`, `worker`, `task_id` | the claim, or a refusal naming why |
| `queue_heartbeat` | `project_dir`, `worker`, `task_id` | extended `lease_expires_at` |
| `queue_submit` | `project_dir`, `worker`, `task_id`, `outcome`, `verify_path`, `verify_exit`, `notes` | acknowledgement |

Workers get exactly three tools, none of which can alter another worker's row. **[UNENFORCED]** An
MCP tool reaches a worker only if that worker's agent-type definition lists it; nothing in this
repository can verify a host's agent configuration, so a misconfigured worker fails at claim time
rather than at launch.

## 7. Scheduling

### 7.1 Eligibility and delegability

A task is **eligible** when every id in its `depends_on` is `DONE` in canonical state.

A task is **delegable** when both hold:

1. `effect.kind` is `none` or `local_write`. `destructive` and `external` effects stay with the
   coordinator, because authorization must be confirmed immediately before the action [C14] and
   confirmed as explicit, current, and exactly scoped at the moment the task starts [C15]. A
   subagent cannot hold that conversation with the user.
2. Its declared outputs share no path with any other currently claimable or claimed task.

Both facts already live in canonical state, so **nothing new is declared and no existing project
needs editing** — a planner who declares outputs properly gets delegation for free. Delegability is
computed, cached in `task.delegable` at seed time, and rechecked against live claims by `queue_ready`.

**[UNENFORCED]** A task that writes something it never declared defeats this entirely, and the
inference cannot detect it. The mitigation is to make undeclared outputs fail loudly elsewhere:
validation already refuses to accept an output reference that does not resolve, and the successor
project should add a post-task check that the worker's touched paths are a subset of its declared
ones. Until that exists, output declaration is a promise.

### 7.2 Leases, heartbeats, reclamation

A claim is granted with `lease_expires_at = now + lease_seconds` (default 600). A worker calls
`queue_heartbeat` at least every `lease_seconds / 3`. `queue_expire_leases` releases any claim whose
lease has passed and writes an `expire` event; the task returns to claimable.

**[UNENFORCED]** A heartbeat proves a worker is running, not that it is making progress. A worker
looping usefully-looking work will hold its lease indefinitely. No timeout can distinguish the two,
so the coordinator's own judgement remains the backstop and the `event` table exists so that
judgement has a record to read.

### 7.3 Concurrency bound

**At most 4 live leases.** Fixed, not configurable. §2 shows 4 is within 0.04x of unlimited across
every measured project while mean maximum level width is 3.87, so a higher bound buys nothing
measurable and costs a wider blast radius on a bad batch and more concurrent writers in one tree. A
configurable bound would be a knob tuned without evidence.

### 7.4 The ready queue, and the wave-synchronous fallback

**Primary discipline — ready queue.** A worker claims the next eligible, delegable task the moment
its dependencies are `DONE`, independent of level boundaries. The coordinator drains results as they
arrive, commits, and reseeds. Nothing waits for the slowest member of a level, which is where most of
the modelled speedup is otherwise lost given a mean level width of 1.86.

The cost is honest and worth stating: committing on result arrival moves the commit count from about
`N + 1` toward one per task. `SKILL.md` treats batching as the economical shape, and it is right to.
The justification is that a status must be true when it is written: under a ready queue results
genuinely arrive one at a time, so a batched commit would either record work as finished before it
was, or delay recording work that was. Correct records cost commits.

**Fallback — wave-synchronous.** Launch a whole level, wait for all of it, commit once, launch the
next. This is also what runs where there is no subagent primitive at all (§10), so one mechanism
covers both the degraded host and the degraded-confidence case: with a fan-out of 1 it *is* sequential
execution, which means the fallback is not a separate code path to rot.

## 8. Authoring wider plans

The measurement in §2 says the executor is not the bottleneck, so this section is load-bearing rather
than advisory. `SKILL.md` already tells the planner to use a graph only where independent work can run
in parallel and to assign non-overlapping outputs [C7]. What it does not say is how to find that
independent work. Four patterns account for most of the avoidable narrowing:

1. **A false chain through a shared artifact.** Three tasks each appending a section to one document
   are ordered only because they name the same output path. Give each its own file and add an assembly
   task: three tasks at one level plus a level-1 join, instead of a chain of three.
2. **Verification folded into a successor.** A task whose verification is owned by a later task cannot
   be delegated, and cannot even reach `DONE` independently — the commit refuses a `RUNNING` dependent
   of a non-`DONE` dependency. Give every task a check it can run itself.
3. **A survey serialized by habit.** Reading five subjects to compare them is five independent tasks,
   and it is the shape that benefits most from context economy: five workers' worth of file contents
   never enter the coordinator's window.
4. **Setup tasks that are actually independent.** Fetching, branching, and scaffolding are often
   ordered by narrative rather than necessity.

And the counter-rule, because this guidance is easy to over-apply: **a plan that cannot be widened
should not be.** The project that produced this document is 5 tasks in 5 levels for good reasons
(§2). Splitting a document into artificial pieces to raise average level width would trade a real
property — one coherent artifact, one author, one review — for a metric.

## 9. The worker contract

A worker receives a prompt containing: its task id, name, success criteria and verification command
verbatim from canonical state; its declared output paths, as the complete list of what it may write;
its lease duration and heartbeat obligation; and the two prohibitions.

The prohibitions are absolute:

- **Never write canonical state.** Not `project.json`, not `spec.md`, not `evidence.md`, not
  `INDEX.md`, not `MEMORY.md`. The worker has no reason to run `commit` or `record-evidence` and no
  tool that would let it.
- **Never write outside the declared output paths.**

A worker returns through `queue_submit` only: an outcome, the path of its verification transcript, the
exit code it observed, and notes. **[UNENFORCED]** Both prohibitions are prompt text. A worker that
disregards them corrupts a sibling's work, and only the post-task path check proposed in §7.1 would
catch it.

Isolation is **the shared working tree**, not a git worktree per worker. Worktrees would make
disjointness a filesystem property rather than a promise, which is strictly stronger — and they were
rejected anyway, because several projects in the reference workspace target plain directories rather
than repositories, and because per-worker worktrees turn result integration into a merge that can
conflict and needs a protocol of its own. §14 records this as revisitable.

## 10. Evidence integration

**The coordinator records all evidence.** No worker runs `record-evidence`, so no worker ever takes
`.project.lock` (§4, I4). Two classes result, and the difference between them must appear in the
closing report.

**Class A — re-run.** The coordinator runs the task's stated verification itself through
`record-evidence`. The recorded entry is composed from a completed process: its real exit code and a
real tail of its real output. This is the default and the strongest class.

Its cost is double execution: the worker ran the check, and the coordinator runs it again. For cheap
checks this is the right trade — an entry that is a record rather than a relayed claim.

**Class B — attested by artifact.** Where verification is expensive, the worker redirects its output
and exit code to a path **inside its own declared output set**, and the coordinator records a cheap
assertion over that artifact: a command that exits non-zero unless the transcript reports success.
`result.verify_path` and `result.verify_exit` are where that lands.

What makes Class B sound rather than a compromise: **the worker is already trusted to write its
declared outputs, so a verification transcript is just another declared output.** No new trust is
extended, and the coordinator's own recorded command is still a real command with a reachable failure
state.

**[UNENFORCED]** Class B is weaker than Class A and the report must say which class each entry is.
The coordinator verifies that the transcript reports success, not that the check itself was sound; a
worker that wrote a passing transcript without running anything would be believed. **[UNENFORCED]**
And "expensive" is a judgement no tool can check, so the choice between classes rests with the
coordinator.

Because the coordinator records evidence after integrating a result, the timestamps in `evidence.md`
are *record* times, not work times. Any parallelism figure derived from them measures integration.
This is why §11 exists.

## 11. Timing, and the canonical-state change

The task schema carries no start, no end, and no duration; a span is derived from the stamps
`record_evidence` wrote, and the code says so — it "measures verification, and only for tasks whose
verification was recorded at all" [C16]. Under this protocol that derivation gets *worse*, per §10.

So two fields are added to a task: `started_at` and `ended_at`, timezone-aware RFC 3339 or null. The
queue supplies them — `claim.claimed_at` and `result.submitted_at` are real worker-side stamps — and
the coordinator copies them into canonical state at commit. **The queue measures; `project.json`
records.**

This requires a schema change with one non-obvious prerequisite. `TASK_FIELDS` [C17] is used as both
the required set and the allowed set in the same two lines: `_missing_fields(task, TASK_FIELDS, ...)`
followed by `_unexpected_fields(task, TASK_FIELDS, ...)` [C18]. A field cannot be optional while
those share one set, and every project written before the change would fail the missing-field check.
So the successor project must first split them:

```python
TASK_FIELDS = {...}                                  # allowed
TASK_REQUIRED_FIELDS = TASK_FIELDS - {"started_at", "ended_at"}   # required
```

with `_missing_fields` taking the required set and `_unexpected_fields` the allowed set. Old projects
then validate unchanged, and a task without timing carries nulls rather than an error. Whether this
warrants a `schema_version` bump is the successor project's decision; the split itself is backward
compatible, which is the argument that it does not.

## 12. Failure semantics

On any task returning `failed` or `blocked`:

1. The coordinator **stops issuing claims** immediately.
2. Tasks with **live leases run to completion** and their results are drained and committed. Nothing
   in flight is cancelled — a half-finished task that has already written some of its declared
   outputs is worse than a finished one, and cancellation would leave the tree in a state no record
   describes.
3. The coordinator then **stops and reports**: the failed task with its evidence, every sibling that
   completed, and every task never started.

Under the wave-synchronous fallback this is the same rule read at level granularity: the level
finishes, the next never launches.

The project moves to `BLOCKED` with a `block_reason` on the failed task. Sibling work that completed
is committed as `DONE` — it happened, and discarding a true record to make a tidier failure story is
the one thing this protocol may never do.

## 13. What this design does not enforce

Collected so that no reader has to infer it. Each appears marked **[UNENFORCED]** where it is stated.

| # | Rule | Why code cannot enforce it | Where |
|---|---|---|---|
| U1 | A worker writes only its declared outputs | Prompt text; the filesystem is shared | §9 |
| U2 | Declared outputs are the task's complete write set | An undeclared write is invisible to an inference over declarations | §7.1 |
| U3 | A Class B transcript reflects a check that actually ran | The coordinator reads the transcript, not the execution | §10 |
| U4 | "Expensive verification" justifies Class B | A judgement, not a measurement | §10 |
| U5 | A heartbeat means progress | No timeout distinguishes work from a loop | §7.2 |
| U6 | A worker's agent type exposes the queue tools | Host agent configuration is outside a plugin's reach | §6.2 |

**Why this section exists at all.** While briefing this design, its author recorded a finding that
`SKILL.md` documents a guard — `init` and `research-validate` warning when the working directory
contains this module but the running tools come from elsewhere [C19] — that no code enforced, and
named it "the precise failure mode a design document is most likely to reproduce." The finding was
false. `self_location_warnings` [C20] is called from validation [C21], and it fired on this project's
first `commit --dry-run`. The error was in the search, not the repository: the grep covered the two
CLI entry points and the tests but never the module that defines the function, and `init` — the only
place its output was looked for — is not a caller.

The lesson survives its own counter-example, and lands closer to home: a claim that something is
unenforced is as unverified as a claim that it is enforced. Both need a checked citation. Every claim
in this document has one in §16, and the checker that validates them re-reads each cited range rather
than trusting this table.

Two smaller findings follow from the same correction, recorded here because they are true and cheap
to state: `SKILL.md` [C19] names `init` as a warning site where this project observed none, and omits
`commit`, which warned.

## 14. Rejected alternatives

**A per-task `delegable` field the planner sets.** Nothing inferred, and a task with hidden side
effects could be marked non-delegable. Rejected: it makes the required/allowed split mandatory rather
than incidental, needs a default backfilled into every existing project, and adds a field a planner
can get wrong. Inference from effect and declared outputs uses facts already required to be correct.

**One git worktree per worker.** Strictly stronger isolation — disjointness becomes a filesystem
property. Rejected because several projects in the reference workspace target plain directories rather
than git repositories, so the guarantee would be unavailable exactly where the protocol is most often
used, and because it converts integration into a merge. **Revisitable** if a future capability gate
makes worktree isolation the path where the working directory is a repository.

**A queue canonical for task status, with `project.json` as a projection.** One store, no mirror, no
staleness. Rejected: it rewrites the commit protocol rather than extending it, replaces optimistic
revision checks with SQLite transactions, requires migrating every existing project, and demotes the
auditable JSON record the whole skill is built on.

**A workspace-root-wide queue.** A cross-project view and a natural home for a shared worker pool.
Rejected: it becomes a mutable file shared across projects, so one corruption affects all of them, and
it contends where per-project databases cannot.

**Letting workers run `record-evidence`.** The obvious simplification. Rejected on the measurement in
§4: the 5 s `.project.lock` timeout it would contend for cannot be raised from the CLI (§16).

**The official `mcp` SDK.** Less code, spec-conformant. Rejected on the floor: not importable by stock
`python3` [C11], so it needs a bootstrap whose failure surfaces when the queue is first needed.

**A ready queue without a durable store.** Leases in memory. Rejected: a coordinator that dies
mid-flight loses completed work that was never committed, which is the failure a queue is best placed
to fix.

## 15. Implementation plan for the successor project

Ordered by dependency, with the split first because two later steps need it.

1. **Split `TASK_FIELDS`** into allowed and required sets (§11) and add `started_at` / `ended_at` as
   nullable. Verify every existing project in the reference workspace still validates.
2. **Build `queue_lib.py`**: schema creation, seeding from canonical state, claim/heartbeat/submit
   with the I3 statements, lease expiry, drain. Stdlib-only, Python 3.9 compatible.
3. **Build the MCP server**: JSON-RPC over stdio, `initialize` / `tools/list` / `tools/call`, the
   eight tools in §6.2. Full coverage [C13].
4. **Wire three hosts**: the Claude Code, Kimi, and Codex marketplace declarations, plus the agent-type
   definition that grants a worker exactly the three worker tools. Test each host surface
   independently; success in one implies nothing about the others [C10].
5. **Add the post-task path check** proposed in §7.1, which converts U1 and U2 from promises into
   detections. This is the highest-value item in the list and the easiest to defer.
6. **Write the `SKILL.md` step** that asks for delegation. Until this exists the protocol is
   unreachable: an agent may not spawn subagents unprompted, so the skill text asking for them is what
   makes the whole design in-policy rather than an interesting document.
7. **Link this reference** from `SKILL.md`, add it to the contract-test `SURFACES` tuple, and bump the
   plugin version so installed hosts receive it.

Steps 1–5 are testable without any host. Step 6 is the one that changes behaviour, and it should be
last for that reason.

## 16. Citations

Every claim above about current behaviour cites a row here. Each row names a file, a line range, and
text that must appear within that range; the checker for this document re-reads each range rather than
trusting the table.

| # | Source | Expected text within range |
|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py:1347-1352` | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py:2768-2790` | `Kahn's algorithm` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py:2791-2793` | `def build_task_graph` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py:2687-2688` | `def levels` |
| C5 | `plugins/research/skills/project/SKILL.md:181-185` | `Workers must not edit canonical state.` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md:11-14` | `sole writer of` |
| C7 | `plugins/research/skills/project/SKILL.md:384-385` | `Use a dependency graph only when independent work can run in parallel` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py:320-350` | `A cross-process lock based on atomic directory creation` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py:2583-2601` | `must not see a half-written one` |
| C10 | `AGENTS.md:43-45` | `must work in Claude Code, Codex, and Kimi Code` |
| C11 | `AGENTS.md:73-76` | `stdlib-only` |
| C12 | `plugins/research/skills/project/references/memory-architecture.md:348-350` | `FTS5 is not guaranteed in a stock` |
| C13 | `pyproject.toml:66` | `fail_under = 100` |
| C14 | `plugins/research/skills/project/SKILL.md:39-41` | `authorization immediately before` |
| C15 | `plugins/research/skills/project/SKILL.md:431-433` | `explicit, current, and scoped to the exact action` |
| C16 | `plugins/research/skills/project/scripts/workspace_lib.py:2612-2615` | `holds no start, no end, and no duration` |
| C17 | `plugins/research/skills/project/scripts/workspace_lib.py:82-96` | `TASK_FIELDS = {` |
| C18 | `plugins/research/skills/project/scripts/workspace_lib.py:1266-1267` | `_unexpected_fields(task, TASK_FIELDS` |
| C19 | `plugins/research/skills/project/SKILL.md:142-146` | `warn when the working directory contains` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py:1667-1669` | `def self_location_warnings` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py:1203-1204` | `self_location_warnings(working_directory)` |

One claim in this document is deliberately **not** citable as a line range: that `record-evidence` is
the only mutating subcommand without a `--lock-timeout` flag. An absence has no line. Its checker
assertion inspects the `record-evidence` subparser and fails if the flag is ever added, which is the
form a negative claim has to take.
