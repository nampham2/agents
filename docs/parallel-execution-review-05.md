# Parallel execution — review 05 and implementation handoff

Reviewed on 2026-09-11 against `44f138840d7564e2498c88c73f2a81c566113d7b`
(design revision 5). The worktree was clean before this review.

Reviewed: the [design](../plugins/research/skills/project/references/parallel-execution.md),
[decisions](parallel-execution-decisions.md), both design test modules, and relevant canonical
validation/transaction code. Line numbers refer to that commit.

## Recommendation

**Close the broad architecture discussion and proceed with a small, inactive Phase 1. Do not treat
the current document as an implementation-ready specification for the entire executor.**

The direction is sufficiently clear: coordinator-owned scheduling, explicit resource claims, durable
attempt records, immutable evidence, conservative recovery, and opt-in concurrency. Another wholesale
rewrite is unlikely to be the best next step. The remaining problems should become explicit entry
criteria for the implementation phase that encounters them.

The important distinction is:

| Decision | Recommendation |
|---|---|
| Start a pure resource-conflict library | Yes; detailed Phase 1 scope below |
| Freeze the broad architectural direction | Yes, subject to the host-feasibility checkpoint |
| Freeze every record schema and operation as written | No; several ordinary paths contradict one another |
| Migrate real projects or activate a new sequential executor | Not yet |
| Enable parallel agents | Only after adapter, recovery, and acceptance gates pass |

Phase 1 should be one small pull request with no user-visible behavior change. Review that result
before commissioning Phase 2. Later phases below are a roadmap, not authorization to implement them
all now. This review itself changes no implementation or design files.

## What is now worth retaining

- The invariant ledger gives implementation reviews concrete questions to answer.
- Requiring predecessor death before takeover is a useful conservative initial boundary.
- Recording a reservation before its grant, and authorizing release before grant removal, are the
  right ordering directions.
- Execution scopes distinguish completion/publication from the right to release resources.
- Explicit task and check read/write declarations improve the resource model substantially.
- Evidence is now compared to an acceptance snapshot, and expected exit status replaces a free
  command verdict.
- Advisory heartbeats no longer create an ever-growing journal.
- The stateful model is a better foundation than the previous label-only model.

These are architectural improvements, not proof that the individual operation contracts are complete.

## Remaining findings, assigned to implementation gates

The following are not a demand to finish every detail before Phase 1. P1 findings block the affected
runtime feature; P2 findings affect model reliability, feasibility, or planning. “Before Phase N” means
resolve it in that phase's short design/specification review before building the dependent feature.

### R5-01 — P1: One immutable acceptance file cannot represent the attempt's canonical mutations

**Design:** §7.4, lines 474–483; §8.1–§8.3, lines 563–639.
**Gate:** before Phase 4's lifecycle runner and Phase 5's canonical integration.

O4 dispatch uses the common intent/commit/ack sequence, writing `acceptance.json` with intent RUNNING.
O11 later needs the same immutable file for intent DONE/BLOCKED. Different bytes at that path are a
conflict. `commit-observed.json` has the same reuse problem. Moreover, the declared acceptance enum
does not include RUNNING, although the O4 recovery row requires it.

This blocks even a successful single-task run; it is not an exotic crash case.

The acceptance also freezes `expected_revision`, `expected_run`, and `expected_generation`. Its own
dispatch commit advances the revision, an unrelated task can advance it again, and takeover changes
run/generation. O12 demands equality with the frozen values, but the intent cannot be rewritten.
Recovery therefore has no specified way to commit an otherwise valid receipt after an ordinary revision
conflict or owner change.

**Recommended resolution:** give each canonical mutation its own stable identity and intent/ack pair.
For example, dispatch and completion have distinct mutation ids under the same attempt. Keep the
completion evidence receipt immutable and separate from the transient commit request's revision and
owner fences. On a conflict, reload and revalidate task definition, attempt binding, authorization and
receipt presence; submit the same semantic intent with fresh expected revision only if still valid.

Do not merely add RUNNING to the enum. The single-path lifetime is the substantive problem.

**Required test:** one attempt dispatches and completes; two attempts complete in either order;
acceptance is published, ownership changes, and recovery commits it once without changing its meaning.

### R5-02 — P1: Capture identity and assessment still contradict immutable publication

**Design:** §7.3, lines 433–441; §11.2, lines 963–979; §11.5, lines 1107–1110.
**Gate:** before Phase 3 freezes record schemas.

`body_digest` hashes the record with only `body_digest` removed. `capture_id` is derived from that
digest and also appears inside the record. Thus the digest includes the id derived from itself. This
is a hash fixed-point requirement, not an ordinary content-addressing procedure.

Separately, worker captures are immutable and published with `assessor: null`; the coordinator is
then instructed to “fill it in.” Changing assessor/adequacy/rationale would change both the bytes and
the content-derived identity. If the intended home is `classification`, its schema needs to contain
the assessment rather than instructing an update to the original capture.

**Recommended resolution:** hash a precisely defined payload excluding all derived identity fields.
Keep process captures immutable; publish assessments as separate immutable records referencing capture
digests. Define which record carries each decision. Give capture, assessment, and receipt validators
literal positive and negative fixtures before exposing any writer API.

**Required test:** construct a capture id in one pass, re-read and validate it, add an assessment
without changing capture bytes, and resolve the accepted evidence chain without an implicit index.

### R5-03 — P1: Stop and failure-budget semantics can still permit new work

**Design:** §10.2, lines 842–876; §14.2–§14.3, lines 1332–1364.
**Gate:** before Phase 4.

The project-wide gate explicitly excludes `operator_stop`. The ready loop mentions `stop_requested`
but gives it no durable source and does not exclude new dispatch while stopped work remains running.
After restart, an operator stop can therefore be treated as something to settle while unrelated tasks
remain eligible. Excluding stops from a *failure counter* is reasonable; excluding them from all
dispatch prohibition is not.

The failure budget resets on any `commit-observed`. Under O4, dispatch itself writes that record.
Repeated attempts can reset their failure history simply by starting, before doing successful work.
The text also scans a trailing run of retry/rerun resolutions while saying block/withdraw terminate
the run but do not reset it; those are not one algorithm.

**Recommended resolution:** make run-stop intent an explicit durable gate, independent of failure
diagnosis. Define a success event narrowly: a committed successful completion receipt, not a dispatch
acknowledgement. Order budget events by durable sequence or canonical decision order, not ambiguous
second-resolution wall-clock timestamps. State exactly which resolution clears which gate.

**Required test:** stop with two tasks active and a third ready, crash, recover, and confirm the third
never starts. Three failures with intervening dispatch acknowledgements must still reach the budget.

### R5-04 — P1: Scope identity and asynchronous event recording remain incomplete

**Design:** §6.1, lines 286–291; O5/O10/O16; §8.2; §11.4.
**Gate:** before Phase 4, then validate against the Phase 2 adapter.

Coordinator checks use scope `check:<check-id>` and one immutable seal path, but reruns may execute
that same check up to sixteen times. A seal for execution 1 must not make execution 2 appear closed.
O10 has no durable scope-start effect before running the subprocess; its listed effects start with
publishing a capture, after the execution has already happened. A crash during the check has no
explicit start record to recover.

O5 requires `launched` or `uncertain_start`. A fast worker can publish before the caller returns from
`start` and writes `launch`. At that point the grant is issued, but neither fact is necessarily true.
Deleting the label graph did not itself remove this precondition problem.

Prelaunch cleanup is also contradictory: the O2 crash row calls O16 when a plan changed, but its
capability is `none`, and O16 permits only `consumed` or `revoked`. No ordinary prelaunch revoke
operation is specified. O13 requires an open cause “resolved withdraw,” although a resolved cause is
excluded from `open_causes` by definition.

**Recommended resolution:** assign one execution id per worker/check invocation, including each rerun.
Durably declare its execution scope before calling the adapter. Separate “issued and never replayable”
from “observed launched.” Permit recording valid early/late results without treating publication as a
seal. Add an explicit never-issued preparation-disposal path and one executable resolution algorithm.

**Required tests:** fast result before launch record; crash during a coordinator check; rerun after
an earlier seal; failed preparation with capability none; withdrawal after its resolution is recorded.

### R5-05 — P1: Ownership establishment and journal fencing are not yet implementable as stated

**Design:** O17; §12.1–§12.4; §13.1.
**Gate:** before Phase 4; resolve the coordinator's concrete lifetime in Phase 2.

Initial activation sets `coordinator_run: null`. Startup offers “already this process” or takeover
with proof of predecessor death, but has no initial-acquisition operation. O17 then wants to change
run/generation while §12.3 says the candidate's run/generation must equal the existing values. A
mutation needs separate expected-old values and desired-new values; one field cannot do both jobs.

Publish-if-absent is still described as a journal fence. It prevents replacement, not a stale writer's
first publication into an empty path. Such a write returns `created`, and an explained historical
generation passes §6.3. The promised exhaustive outcomes—identical, conflict, or indeterminate—omit
this fourth case. Proven-dead takeover can exclude the old coordinator within its stated assumptions;
that is a different justification and should be used explicitly.

The implementation also needs an identified coordinator process. A short-lived CLI command's pid is
not the lifetime of a conversational agent/session calling that command. Do not use command exit as
proof that the logical dispatcher cannot issue another tool call. The design must choose a long-lived
runner or a host session identity with the needed liveness contract.

**Recommended resolution:** specify initial acquisition, graceful owner release, and takeover as
dedicated canonical operations with expected-old fences. Document what process/session owns execution.
Define allowed historical worker publications separately from coordinator decisions; do not attribute
authorship or generation exclusion to hard-link publication.

**Required tests:** first run from null ownership, two simultaneous acquisition attempts, owner crash,
fresh revision with stale run identity, and a stale first publication rather than only clobbering.

### R5-06 — P1: Several data contracts cannot represent the advertised work

**Design:** §7.6; §9.1/§9.4; §10.1; §16.3.
**Gate:** before Phase 3 schemas and Phase 5 migration. None blocks the pure Phase 1 module.

The concrete mismatches to fix are:

- `effect.confined_to` is required by admission/authoring, but canonical effects allow only `kind`
  and `description`. Schema v4 explicitly promises no task-field changes. A direct probe of the real
  validator rejects `confined_to`. Put this execution-specific declaration in the execution plan, or
  deliberately design a schema change; do not assume the field already exists.
- Baseline records only read claims and deliberately removes read/write overlap. Reconciliation then
  compares output writes against that baseline to distinguish created/modified/unchanged/deleted.
  Writable subjects need their own before-state snapshot, as an earlier revision already recognized.
- Nonenumerable, directory, and external work is both outside the envelope and routed inline through
  the same contract. That contract still requires concrete local paths, regular-file digests and
  scoped claims. Lowering capacity does not supply missing reference/snapshot semantics.
- The worker's step list runs checks and publishes results, but does not specify the task execution
  step or a complete work instruction. A check runner is not a general agent-task executor.
- Required-null fields and result shapes differ between §7.4, §11.2, §16.3 and §16.4. For example,
  results are digest maps in one place and tags plus `checks_run`/`baseline_matched` in another.

**Recommended resolution:** freeze only the supported local-file task shape for the first runtime
pilot. Unsupported work should pause with a clear action, not silently enter an undefined fallback.
Keep existing unactivated v3 sequential behavior unchanged. Define execution instructions separately
from verification commands, and use complete example records as the source for validators/tests.

### R5-07 — P2: The new model improves structure but still tests a different protocol

**Files:** `tests/plugins/research/test_parallel_execution_model.py`, especially lines 176–227,
271–283, 452–527 and 529–568.
**Gate:** before Phase 4 is accepted as a lifecycle foundation.

The model's capability becomes consumed when a result appears; the design consumes it after recording
the start outcome. The model treats a release record as removal of the grant, omitting the design's
separate registry-removal prefix. Many operation ids/effects differ, and the ten scenario entries do
not cover all seventeen specified operations. Those abstractions remove exactly the boundaries the
recovery tests are supposed to check.

Targeted probes also show substantive gaps:

| Probe | Observed result |
|---|---|
| Existing record whose JSON value is null | Projects as `absent`, contrary to I5 |
| Live worker has published result but no seal | `refuse_release` returns no refusal; applying release violates I2 |
| Publish acceptance intent, change output bytes, then recover | Commits DONE and reports no invariant violation |
| DONE task with evidence `not-a-receipt` | Model reports no invariant violation |
| Unresolved failure on task A; reserve unrelated task B | Model does not apply the documented project-wide gate |

The acceptance check is attached to publishing the intent, not `CommitCanonical`, so it misses a
change between intent and commit. Recovery applies commits without their fencing/revalidation guards.
The real-validator harness is useful, but tests v3 candidates without actual output declarations,
not v4 execution binding or recovery. Its passing result should be described at that narrower scope.

**Recommended resolution:** retain the world/store separation but align operation effects and guards
with the reviewed phase's implementation. Call guards from one operation entry point used by tests
and recovery. Check invariants at the actual canonical mutation and resource-release effects. Keep
unsafe-effect injection only as an explicitly separate negative-test API.

### R5-08 — P2: The first real execution target is unresolved

**Design:** §16.5, §17.1 and §20.
**Gate:** Phase 2 go/no-go, before investing in a production lifecycle stack.

The design explicitly says no current host adapter is verified, Claude Code runs inline, and the
candidate for asynchronous operation is a local subprocess adapter. Starting a local process does not
by itself implement the original objective: delegating agent tasks with separate context. The process
needs a concrete agent entry point, task instructions, result delivery, and lifecycle ownership.

The inline restriction is also a significant product change: every criteria-based inline task now
requires a human assessor, even where the original task did not require independent/human review.
This is stronger than the earlier recommendation to honor the task's declared independence policy.
It should not become a universal default merely to simplify the protocol.

**Recommended resolution:** make host feasibility an early, bounded checkpoint. Pick one actual host
and one new-file analysis task, and show how it runs and becomes safely non-writing under the selected
trust model. A subprocess used only for tests is an integration fixture, not evidence of parallel-agent
delivery. Keep required human/independent assessment explicit in the plan rather than inventing it for
every task. If no adapter meets the selected contract, stop runtime implementation and report that
limitation; do not migrate users into a more complicated sequential-only workflow by default.

This review makes no new claim about current host capabilities. The finding is the unresolved delivery
path admitted by the design itself; no installed-host conformance tests were run here.

### R5-09 — P2: The measurement does not justify the stated capacity or rollout gate

**Design:** §3.2, §10.2–§10.3, §18 and §21.3.
**Gate:** documentation correction now; performance evidence before Phase 6.

The table says 31 projects and **mean maximum** width 3.87. Later sections say 26 graphs, median
width 1.42, and claim capacity four is never binding because maximum width is 3.87. A mean of maxima
does not bound any individual project's maximum. The table itself gives 1.41x at four workers versus
1.45x unlimited, so “never binding” contradicts the stated measurement.

The structural graph harness also cannot decide whether real concurrency is worthwhile: it omits
spawning, journal work, verification, integration, cost and context behavior. Stage 8 should not be
enabled or rejected on graph width alone.

**Recommended resolution:** use a conservative provisional cap of two for the pilot, no optimality
claim, and retain four only as a later configurable bound justified by actual measurements. Reproduce
or remove the historical figures. Benchmark real equivalent sequential/parallel runs, including agent
tokens, total cost, quality and failure handling—not just graph shape.

## Implementation phases

These phase numbers replace the rollout order recommended by design §20; they are deliberately
separated by review gates. A phase may reveal that a later phase is not worth building.

| Phase | Deliverable | What remains disabled | Exit review |
|---|---|---|---|
| 1 | Pure resource-claim comparison library and tests | All execution, disk state and CLI integration | Verify the small API and conflict matrix |
| 2 | One-host feasibility spike using disposable fixtures | Real-project migration and production dispatch | Demonstrate an actual agent task and defensible lifetime/stop behavior |
| 3 | Record schemas and tested filesystem publication library | Canonical mutation and task execution | Validate identities, immutable assessment, and real filesystem failure prefixes |
| 4 | Guarded lifecycle/recovery runner against fake adapters and a test store | User-facing activation | Successful, failed, stopped and interrupted runs obey the same reviewed operations |
| 5 | Explicit v4 migration and opt-in sequential integration in disposable/pilot projects | Concurrency | Canonical commit, evidence, ownership and old-reader behavior work across affected hosts |
| 6 | Opt-in two-attempt parallel pilot, then consider four | Unsupported task shapes, unattended ambiguous retries | Real overlap is safe, useful and measured |

### Phase 1 — a deliberately small first PR

**Goal:** implement the stable resource-conflict semantics without committing to unfinished journal or
host APIs. This is useful code that later phases consume, not a production scheduler.

Suggested files:

```text
plugins/research/skills/project/scripts/execution_claims.py
tests/plugins/research/project/test_execution_claims.py
```

Use one immutable, typed claim representation with explicit `namespace`, canonical `key`, and
`access` (`read` or `write`). The current design describes string claims but stores read/write mode
elsewhere; this small API makes that distinction unambiguous without changing on-disk schemas.

Suggested public surface, illustrative rather than a prescribed naming convention:

```python
normalize_claims(claims) -> tuple[Claim, ...]
claims_conflict(left: Claim, right: Claim) -> bool
find_conflicts(requested, held) -> tuple[Conflict, ...]
```

Implementation boundaries:

1. Accept **already resolved comparison keys**, supplied by a future filesystem/plan adapter. Do not
   probe volumes, call `Path.resolve()`, inspect symlinks, hash files, or infer resources from commands.
   Document that callers own actual filesystem identity and case normalization. This module proves
   only comparisons over its input identities.
2. Validate input shape and reject unknown namespaces/access values, empty keys, relative local keys,
   parent traversal and NULs. Be explicit about the accepted canonical local-path spelling; do not
   quietly support two spellings as different resources. A bad input must produce a deliberate,
   tested validation error, not an accidental traceback from indexing or iteration.
3. Compare local paths by components: `/repo/a` is an ancestor of `/repo/a/b`, but `/repo/a` is not an
   ancestor of `/repo/ab`. Two read claims never conflict. Any write conflicts with equal/ancestor/
   descendant claims from another grant.
4. Treat `store:<attempt-id>` as exact-key identity. Do not apply filesystem ancestry to an opaque
   attempt id. Claims in different namespaces do not conflict at this layer. Reject the currently
   unsupported external namespace rather than pretending to implement it.
5. Normalize duplicate identical keys deterministically; write subsumes read. Preserve distinct
   parent/child declarations unless there is a separately tested need to minimize them. Do not add a
   trie or interval index for a four-attempt use case.
6. Return reproducible conflict pairs, so later admission can explain which requested resource is
   blocked. Compare different grants at the caller; an attempt's own read/write combination is not
   a request for a second reservation.

**Required tests:** read/read, read/write, write/write; equal keys; ancestor in both directions;
prefix siblings; distinct roots; local/store separation; equal/different store ids; malformed inputs;
duplicate normalization; write-dominates-read; deterministic output; symmetry; and idempotent
normalization. Case-insensitive aliases should be tested using equal pre-normalized comparison keys,
without claiming this module detects filesystem aliases itself.

Keep it stdlib-only and Python 3.9-compatible, with typed public functions. Do not import the design
test model into shipped code. Do not refactor `workspace_lib.py` or the launchers for this PR. The
proposed script directory is already covered by `tests/conftest.py` and the type-checker extra path,
so no path configuration change should be needed.

**Explicit non-goals:** no registry directory, locks, durable writes, capture schemas, schema v4,
plan-text interpretation, CLI command, `SKILL.md` activation, host API, background process, migration,
automatic retry, or change to sequential execution. If the first PR needs any of these, split it.

**Definition of done:**

- The small conflict API and documented preconditions are independently understandable.
- All new shipped statements are covered; preserve the repository's 100% coverage gate.
- Run `uv run pytest -q`, `uv run ruff check .`, `uv run ty check .`, and `uv lock --check`.
- Verify Python 3.9 compatibility in an available runtime/CI job; do not confuse the dev toolchain's
  Python version with the shipped script requirement.
- No runtime caller imports/enables the new module yet; no project file changes as a result of
  existing commands.
- Review the PR and stop. Later phases are not implicit follow-on work.

No release bump is needed merely to submit an inactive implementation PR unless repository release
policy calls for publishing it. Any actual plugin release must use the normal synchronized version
bump and host update process; editing the checkout never updates installed copies.

### Phase 2 — prove the delivery path before building the framework

Use one host, one disposable project, and one bounded task that creates a new file. Prove task
instructions reach an actual agent; execution is distinguishable from verification; output/evidence
returns; and stop/completion can be interpreted under the declared scope rules. Identify the logical
coordinator's lifetime and what a successor can observe after it disappears.

This is a feasibility experiment, not a general host adapter implementation. Record what is observed,
what is merely a cooperative rule, and what remains unavailable. Do not accept a shell-only command
fixture as satisfying the separate-agent/context objective. Address R5-05/R5-08 here. If successful,
freeze the minimal adapter contract for subsequent phases; otherwise stop or revise only that boundary.

### Phase 3 — real storage primitives, then schemas

Split this phase into two reviewable changes if needed: the byte-publication helper first, then typed
record encoding/validation. Settle R5-02/R5-06 before declaring persistent formats stable. Use random
or payload-derived ids with no self-reference; keep observations and assessments separate. Reject
malformed arbitrary JSON without treating it as absence.

Use temporary directories to test create/identical/conflict, preservation of original bytes, missing
ancestors, short writes, file/directory sync failures and retry after publication before acknowledgement.
Keep `atomic_write_text` unchanged for its existing replacement callers. State the precise durability
boundary rather than claiming power-loss testing from injected Python exceptions. No real project
activation or collection is needed; defer garbage collection until retention invariants are proven.

### Phase 4 — executable operations and recovery, not another parallel prose model

Resolve R5-01/R5-03/R5-04/R5-05 and align the reference model with the exact operations implemented.
Use unique mutation ids, per-invocation scopes, explicit stop intent, and one guarded entry point.
Apply every recovery action through that same entry point. A fake adapter controls delayed starts,
unknown outcomes, child scopes, failed checks, late results and termination attestations.

Exit only when tests cover complete successful/failed runs, all implemented durable prefixes, crashes
during recovery, two competing owner acquisitions, nonconflicting completions at different revisions,
and no dispatch after stop. Invariants must be checked at actual effects, including canonical commit,
not only at intent publication. Fix the concrete model probes in R5-07 rather than increasing counts.

### Phase 5 — explicit activation and sequential integration

Add a dedicated migration path; changing `schema_version` is an exception to the ordinary immutable-
field rule, not an ordinary candidate update. Preserve authorization and terminal history, write
`project.json` last, retain post-commit index recovery, and handle config-written/state-not-committed
prefixes. Separate initial owner acquisition from takeover.

Integrate the proven adapter at capacity one in disposable projects first. Test complete canonical
candidates, receipt append/dedup, active-attempt binding, graceful owner release, reopening and old-
reader refusal on all affected host surfaces. Preserve existing unactivated v3 behavior. Do not
activate live projects on an installation that can read v4 but cannot execute/recover it safely.

This phase is behavior-changing and merits its own implementation review and opt-in rollout. Do not
ship schema migration as a standalone user feature before a working recovery/execution path exists.

### Phase 6 — bounded parallel pilot and measurement

Start with two independent, new-file tasks, not arbitrary repository editing. Test actual overlap,
conflicting read/write exclusion, failed sibling handling, stop during execution, and recovery without
double start or double receipt import. Exercise two projects sharing the same participating workspace.

Measure sequential versus parallel runs from equivalent starting states, repeat samples, and include
linear, tiny-task and independent-analysis cases. Report wall time, coordinator/worker tokens, total
cost, journal overhead and judged result quality. Decide whether to keep two, try four, or leave
parallelism disabled. Directory hashing, external effects, worktrees, live-owner takeover and automatic
retry of ambiguous execution remain separate future proposals.

## Small corrections to keep in the phase backlog

These do not justify another global design round. Fix them when the owning phase touches the section.

| Detail | Owning phase |
|---|---|
| Specify read/write mode alongside claim strings, namespace comparison and protected-path derivation; do not hide access mode in prose | 1 API; 3 plan schema |
| Validate task/check/cause/scope/owner ids before using them in paths; canonical task ids are not already restricted to filename-safe strings | 3 |
| Define common envelopes separately for project-level owner/config/plan records and attempt-level records; not every record has a task/attempt id | 3 |
| Give every identity-bearing array an actual sort/order rule; “ordered” alone does not specify one | 3 |
| Define log digests, bounded concurrent draining, redaction and all settings the text references (`summary_cap` is used but absent from the settings table) | 3 and 4 |
| Explain mutable grant capability updates: they need locked atomic replacement, not the immutable journal helper | 3 and 4 |
| Define the classification enum; §7.4 points to §14, which gives cause classes rather than a complete classification schema | 3 and 4 |
| Stop using labels in capacity/admission/closure after promising decisions only use facts; the capacity list also omits ACCEPTED, COMMITTED and INDETERMINATE | 4 |
| Use an operation-aware canonical receipt lookup: completion removes the active attempt binding, so success cannot require it still be present | 4 and 5 |
| Define when first ownership is cleared to null so the closure gate can pass | 4 and 5 |
| Do not say “never fsynced” while prescribing `atomic_write_json`, which calls the fsyncing replacement helper | 3 |
| Distinguish an executor returning from a call from a whole process tree exiting; capacity one does not eliminate descendants | 2 and 4 |
| Respect `checks[].executor`; the worker instruction currently says to run every check | 2 and 4 |
| The schema-version field is immutable in ordinary commits; the activation text's “block is new” does not exempt the simultaneous version change | 5 |
| Keep approved resolutions and failed/superseded captures in the auditable retention closure; postpone deletion rather than rebuilding GC now | 5 or later |
| Replace asserted fairness from plan order with a narrow deterministic tie-break promise; repeated resource conflicts can still starve a task | 6 |

The decision record is not reliable enough to act as a completion checklist on its own. For example,
S4-18 describes external key escaping even though external claims are now refused, and S4-28 claims
stream-draining/redaction rules that are not specified in the cited sections. Several explanations
still refer to superseded section numbers. Use current normative text plus passing focused behavioral
tests as each phase's evidence; edit dispositions only after checking the implementation actually
delivers the stated change.

## Validation performed for this review

Against the reviewed checkout:

```sh
.venv/bin/python -B -m pytest -q --no-cov -p no:cacheprovider -o log_cli=false \
  tests/plugins/research/test_parallel_execution_doc.py \
  tests/plugins/research/test_parallel_execution_model.py
# 44 passed, 90 subtests passed in 4.54s

.venv/bin/ruff check --no-cache .
# All checks passed!

.venv/bin/ty check .
# All checks passed!
```

Read-only in-memory probes produced the results in R5-07 and the canonical validator's rejection of
`effect.confined_to`. No production executor, host adapter, full regression/coverage run, power-loss
test, or performance benchmark was exercised. Passing the focused suite does not contradict the
findings: the probes expose boundaries that suite currently omits.

**Handoff:** implement Phase 1 only, review it, then decide Phase 2. Keep the rest of the design as a
phase-gated roadmap rather than continuing an all-or-nothing design-approval cycle.
