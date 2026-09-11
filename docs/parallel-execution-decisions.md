# Parallel execution design — decisions and history

Companion to [`plugins/research/skills/project/references/parallel-execution.md`](../plugins/research/skills/project/references/parallel-execution.md),
which is the normative reference: current rules, operations and failure behaviour.

This document holds everything that is *about* that document rather than in it — the revision
history, the measurement error that selected revision 1's architecture, the disposition of both
expert reviews, and the alternatives that were rejected, deferred or revisited. Two reviews argued
the reference was becoming a response to its own reviews; splitting them is how that stops.

Nothing here is normative. Where this document and the reference disagree, the reference wins and
this one has a bug.

## Revision history

| Revision | Date | What changed, and what caused it |
|---|---|---|
| 1 | 2026-09-11 | First design. A SQLite queue under the workspace root, an MCP server for worker access, wave-synchronous scheduling, and a claimed 2-4x speedup. |
| 2 | 2026-09-11 | Rewritten after [review 01](parallel-execution-review.md). Measured the plans instead of assuming them; replaced the queue with a file-based store inside the project directory; replaced waves with continuous dispatch; withdrew the speedup claim; added the recovery contract, the resource model, the unenforced-guarantees table and a citation table. |
| 3 | 2026-09-11 | Rewritten after [review 02](parallel-execution-review-02.md). Added the attempt transition table, `DISPATCHING`, the publication protocol, the receipt/reference/acknowledgement separation, record schemas with a version gate, the acquisition policy, coordinator run identity and lock order, the versioned contract object and its derivation, the method/actor/capture model, the bounded observation period's terminal action, host-adapter contracts and a retention policy. Deferred staging. Split this document out of the reference. Committed the citation checker as a repository test. |

## The measurement error that produced revision 1

Revision 1's central claim was a 2-4x speedup, and it named a SQLite queue with an MCP server as the
mechanism to reach it. Neither number nor mechanism was wrong because of a coding mistake; they were
wrong because **the workload was never measured before its executor was designed.**

Dependency levels were then computed over all 31 projects in the reference workspace with five or
more tasks. Mean average level width is 1.86, modelled speedup 1.45x mean under a model that charges
nothing for spawning, prompt authoring, integration or contention, and 13 of the 31 projects sit at
or below 1.2x. The upper bound on the honest benefit was roughly a third of what revision 1
advertised, and the concurrency available was small enough that the machinery to exploit it had to be
cheap.

Two conclusions followed, and both are now rules in the reference:

- A queue in a database, reached through a server, is a large mechanism for a mean of 1.86 parallel
  tasks. Revision 2 replaced it with files in the project directory (§5), and moved a database and an
  MCP transport to Stage 8 (§19), to be reconsidered against recorded conditions rather than
  anticipated ones.
- The measurement itself is the argument, so §2 of the reference leads with it, states that the model
  is generous, and states what it does *not* measure — task durations, resource conflicts,
  integration cost, host capacity.

**The lesson worth keeping** is the one already recorded in workspace memory as
`measure-the-shape-before-the-engine`: measure a workload's shape before designing the mechanism whose
payoff depends on it. Revision 1 had a plausible architecture for a workload that does not exist in
this workspace.

## The §3.1 void-lock correction

Revision 1 asserted that `research:project` had "no cross-process locking" and proposed adding it.
That was false: `DirectoryLock` is a `mkdir`-based cross-process lock that has been in
`workspace_lib.py` throughout, and `commit --expected-revision` has always given optimistic
concurrency on top of it.

The correction is small; the reason it happened is not. A design that proposes to *add* a facility
has an obligation to check whether the facility exists, and revision 1 discharged that obligation by
recalling rather than reading. Revision 2 responded structurally: every claim the design makes about
current behaviour now cites a file, a line range, and the literal text expected inside it (§21), and
revision 3 commits the checker that re-reads all of them —
`tests/plugins/research/test_parallel_execution_doc.py` — so the citations are re-verified on every
test run rather than at review time.

**The lesson worth keeping**, recorded as `documenting-for-two-audiences`: verify behaviour against
source before documenting it. A design's claims about the existing system are load-bearing, because
they decide what gets built.

## Review 01 — disposition

[`parallel-execution-review.md`](parallel-execution-review.md). Six high-priority findings, plus a
medium-priority one about locking that revision 2's introduction mistakenly counted among them (see
the smaller corrections below).

| # | Finding | Disposition in revision 2 |
|---|---|---|
| 1 | Speedup claim unsupported | **Accepted.** §2 replaced the claim with a measurement and stated the model's generosity. |
| 2 | SQLite queue and MCP server disproportionate | **Accepted.** File-based store inside the project directory; database and MCP deferred to Stage 8. |
| 3 | Queue placement at the workspace root creates a shared mutable file across projects | **Accepted.** The store moved inside `<project-dir>/execution/`. |
| 4 | Wave-synchronous scheduling wastes the concurrency it has | **Accepted.** Continuous dispatch on satisfied dependencies; levels retained for presentation only. |
| 5 | No recovery contract | **Accepted.** §13's interruption table, one row per interruption point. |
| 6 | No resource model; "non-overlapping outputs" is not a check | **Accepted.** §8.2's claims, namespaces and comparison rules. |
| — | "No cross-process locking" is false (medium) | **Accepted.** See the void-lock correction above. |

Revision 2 also added the unenforced-guarantees table (§18) on review 01's prompting: several of the
design's rules are prompt text, and a design that does not say so implies enforcement it does not
have.

## Review 02 — disposition

[`parallel-execution-review-02.md`](parallel-execution-review-02.md). Ten principal findings — seven
P1, three P2 — nine additional implementation issues, and fourteen smaller corrections.

**Accepted in full on substance.** Every checkable claim in the review was re-verified against source
before disposition, and all ten findings hold. Four points are answered rather than adopted as
written; those are below the table, and each is a disagreement about the fix, never about the defect.

| # | Finding | Disposition in revision 3 |
|---|---|---|
| 1 | A crash during spawning is indistinguishable from never-launched | **Accepted.** Durable `DISPATCHING` before the host call (T3, T4); no handle plus no launch key resolves to `UNCERTAIN` (T6), never to redispatch; §17 requires `spawn` to be idempotent under a launch key, offer discovery, or report a definitive failure, and requires `observe` to state whether `finished` is recursive. |
| 2 | The canonical acceptance record is unspecified | **Accepted.** §7.2 separates receipt, canonical evidence reference and acknowledgement; a receipt carries **no** revision, so its identity cannot change on a lost race; recovery reads acceptance from the canonical reference. §7.2 and U10 state that `_validate_evidence_reference` checks shape and existence only, and Stage 4 owns receipt validation explicitly. |
| 3 | Atomic replacement is not a multi-file immutable publication protocol | **Accepted.** §7.1's six steps: logs and captures first, hash, manifest last; identical republication is success; differing republication is rejected (T11); incomplete publication is distinguishable from a failed result (T10). §5.4 now classifies `runtime.json`, live dispatch records and reservations, and disposability of a dispatch record turns on *established* non-execution. |
| 4 | Reservations held through integration can deadlock on verification upgrade | **Accepted**, taking the review's first option. §8.3 acquires the complete execution-and-verification claim set upfront with a no-upgrade invariant; the two-task scenario is reproduced verbatim in the reference and is a required test (§20.1). The cost — a repository-wide check serializes against every writer — is stated where the rule is, and §16.3 turns it into authoring guidance. |
| 5 | Coordinator takeover is undesigned | **Accepted.** §12.1 defines run identity, ownership generation, and the five operations; every mutation re-checks ownership; a superseded coordinator stops. Ownership generation is explicitly separate from attempt generation (§6.3) so a takeover can adopt in-flight workers. §12.2 states the total lock order. |
| 6 | The contract hash omits execution-relevant state and has no derivation contract | **Accepted.** §6.4 is a versioned object with sixteen named fields including task name, `depends_on`, effect, authorization scope, working directory, resolved roots and mode; serialization, ordering, algorithm and absent-value treatment are specified; §6.5 defines and records the derivation of the three fields canonical tasks do not carry. Authorization revocation is a separate rule re-checked at T4, T12 and T14 — a matching hash never authorizes an action. |
| 7 | Staging does not provide the confinement automatic retry needs | **Accepted.** Staging is **deferred** (§9), automatic retry is unavailable in this revision (§13.1), and its return requires a verified confinement capability plus a promotion protocol. |
| 8 | State-machine paths that cannot release reservations | **Accepted.** §6.2's 25-row transition table with five terminal states; every terminal state releases reservations except `QUARANTINED`, which holds them and also pauses dispatch so nothing starves. A late valid result from `UNCERTAIN` is adopted (T18) or quarantined as superseded (T19), which is the case "live attempt" alone would have rejected. |
| 9 | Bounded shutdown promised with no possible terminal action | **Accepted.** §14.1's five-step terminal action, ending in "exit", with the reference stating plainly that it does not promise reconciliation completes. The pause rule now covers every failure source the review listed, and §14.4 makes recovery scan attempt states rather than trusting `dispatch_paused`. |
| 10 | Verification provenance confused with validity | **Accepted.** The ranking is **removed**, not relabelled. §11.1 is three independent axes with an admissibility table; §11.4 is the adequacy comparison that catches a capture for `true` and a post-check artifact modification. |

The nine additional implementation issues are all addressed: separate `path` / `external` /
`exclusive` namespaces (§8.2); read/write modes with a conflict matrix in which two readers never
conflict; the workspace-root target claim with ancestry, and capacity one on failure to acquire
(§8.5); §17's split between a missing worker capability and a missing correctness prerequisite;
heartbeats reduced to an optional `deadline.heartbeat_seconds` with host observation as the authority
and U6 stating what a heartbeat does not mean; the fail-closed old-reader gate as a `project.json`
`schema_version` bump (§5.3); Stage 5's explicit list of what the capture helper must do differently
from `record_evidence`; one capture directory per verification command with a manifest (§5.1); and
§5.5's retention policy.

### Four points answered rather than adopted

1. **Finding 3 asked for a publication protocol; it did not need a new atomic-write helper.** The
   review's praise of `atomic_write_text` is accurate — it flushes, `fsync`s the descriptor,
   `os.replace`s, and `fsync`s the directory. The gap is ordering across files and immutability,
   which are protocol properties, not helper properties. So §7.1 is a sequence over the existing
   helper, and the reference says so where the helper is introduced (§3) rather than implying a
   missing primitive.

2. **"Versioned JSON schemas" is met by field-level tables and a compatibility rule, not by JSON
   Schema documents.** Literal `.json` schema files would be implementation shipped inside a design
   document, and would need their own validator to mean anything. §5.2 specifies every field's name,
   type and meaning; §5.3 specifies major/minor semantics, additive minors, and unknown majors as
   *unreadable rather than absent*. Stage 4 owns whatever machine-readable form the implementation
   wants.

3. **Finding 10's ranking is replaced rather than re-labelled.** The review's `method` / `actor` /
   `capture` decomposition is adopted exactly, and the "Strongest / Strong / Weakest" ranking is
   deleted rather than rewritten with better labels, because ranking provenance at all is the error
   — a coordinator can run an inadequate command and get a perfect process record. §11.1 states that
   in place of a ranking.

4. **Capacity one stays the sequential path; the refusal is separate.** The review is right that
   capacity one does not restore missing result delivery or persistence. But capacity one is not a
   degraded mode of this protocol — it *is* the sequential path, which is what the skill does today
   and which the reference defines as the same scheduler at capacity one (§4). So §17 splits the two
   cases: a missing worker capability gives an inline executor at capacity one with recovery intact,
   and a missing correctness prerequisite — durable delivery, durable persistence — means **the
   protocol does not start**. Two different words for two different situations, rather than one word
   covering both.

### Corrections to the design's own record

Review 02's smaller corrections are all applied. Three deserve naming, because they are errors about
the design's own history rather than about its rules:

- **Six high-priority findings, not seven.** Revision 2's banner and its commit message both
  miscounted review 01's findings, and the locking finding they partly relied on was *medium*
  priority, not high. Review 01's disposition table above states six plus the medium one. The commit
  message is history and is not rewritten.
- **The checker was not in the committed tree.** Revision 2 described a citation checker that lived
  only in the project workspace, which made a documented check unreproducible by anyone reading the
  branch. It is now `tests/plugins/research/test_parallel_execution_doc.py`, inside the repository
  suite. Committing it outside `plugins/research/skills/project/scripts/` keeps it clear of the
  `fail_under = 100` coverage gate that applies to that package, so a documentation checker does not
  become a coverage obligation.
- **The benchmark does not ship in this branch.** Revision 2 had a present-tense sentence implying
  the graph-shape script and fixtures were committed. They are not. §2 and §20.2 of revision 3 are
  future-tense Stage 2 commitments, and say the figures are reproducible in principle and not yet
  from this repository.

The remaining eleven — durations in seconds rather than UTC, the writer-ownership sentence scoped to
the execution store, read-only workers no longer described as having "nothing to isolate", one rooted
protection policy replacing two disagreeing lists, the exact sort key, the unmeasured "critical
section is short" removed, "the bottleneck is the plan" narrowed to the structural limitation this
sample demonstrates, "the last activation prerequisite" instead of "last", the batching disposition
corrected, and the retired-phrase check given section-scoped exclusions — are applied in place in
revision 3.

### The retired-phrase check, and why it needs exclusions

Review 02 noted that a whole-document prohibition on withdrawn claims would reject the document's own
explanation of them. That is now structural rather than a matter of care: withdrawn claims are
discussed *here*, in a document the reference's checker does not police for them, and the checker
splits its prohibition in two — claims that must appear nowhere at all, and component names that may
appear only in sections that exist to discuss history. Splitting the documents makes the second list
much shorter than it was.

## Rejected, deferred and revisited

| Option | Status | Reasoning |
|---|---|---|
| SQLite queue under the workspace root | **Rejected** | A shared mutable file across projects, and disproportionate to a mean of 1.86 parallel tasks. Revisit at Stage 8 against measured contention. |
| MCP server as the worker transport | **Deferred to Stage 8** | Adds a process and a protocol to reach a queue the coordinator already owns. Reconsider if measurement shows the file store is the bottleneck. |
| Wave-synchronous scheduling | **Rejected** | Idles a worker whose task finished early behind the slowest member of its level; §2's capacity comparison shows continuous dispatch captures nearly all of the available benefit. |
| A separate sequential code path | **Rejected** | It would be the least-exercised code on every degraded host. Sequential execution is this scheduler at capacity one (§4). |
| Workers claiming their own next task | **Rejected** | At four workers the coordinator already owns every decision before and after execution; self-claiming adds a distributed protocol to a problem that has none. |
| Timeout-based coordinator takeover alone | **Rejected** | It can hand ownership to a second coordinator while the first is still running. Replaced by run identity plus ownership generation (§12.1). |
| PID-based ownership | **Rejected** | The CLI process exits while a session-long coordinator keeps working, so a PID lock looks stale while the owner is live. |
| Automatic retry after lost contact | **Deferred** | Requires a verified confinement capability no host currently asserts (§13.1). A missed deadline is not evidence that a worker stopped. |
| Isolated staging directories | **Deferred** | A changed working directory is not confinement, and promotion needs its own protocol (§9). |
| Lock upgrade at verification time | **Rejected** | Deadlocks with two tasks (§8.3). Upfront total acquisition plus quarantine-and-re-prepare instead. |
| Per-task `started_at` / `ended_at` fields | **Deferred** | `TASK_FIELDS` is both the required and the allowed set, so a field cannot be optional until they are split, and an old installation rejects unknown fields anyway. Timing lives in captures and attempt records instead, which makes achieved parallelism measurable with no schema change (§15). |
| A sidecar file in `execution/` as the old-reader marker | **Rejected** | An old installation never looks there. The fail-closed marker is a `project.json` `schema_version` bump, which an old reader refuses outright (§5.3). |
| Commit batching as a concurrency optimization | **Deferred, and no policy is claimed** | The skill already permits one commit to advance several tasks, and revision 2 pointed at two sections that specify no batching policy. Revision 3 states that it remains deferred rather than citing sections that do not cover it. |
| Literal JSON Schema documents in the reference | **Rejected** | Implementation inside a design document, and inert without a validator. Field-level tables plus a compatibility rule instead (§5.2, §5.3). |
| Post-task comparison of touched paths as enforcement | **Rejected as enforcement, kept as diagnostics** | With several concurrent attempts it cannot attribute a change to one of them, and it cannot see a write that was later reverted (U2). |

## What is still unmeasured

Stated here so it is not mistaken for settled:

- **Context economy.** The reference's §1 names it as plausibly the larger benefit and as a
  hypothesis. Coordinator context consumption has never been measured against a sequential baseline.
- **Contention.** Whether locking under concurrency is material is unknown; revision 2's claim that
  "the critical section is short" was removed for being unmeasured rather than wrong.
- **Executor overhead.** Never measured, which is why the reference claims only the structural limit
  its sample demonstrates and makes no comparison between plan shape and executor cost.
- **§2's figures from this repository.** Reproducible in principle; the script and fixtures are a
  Stage 2 commitment.
