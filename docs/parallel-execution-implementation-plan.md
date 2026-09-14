# Parallel execution — phased implementation plan

Companion to [`plugins/research/skills/project/references/parallel-execution.md`](../plugins/research/skills/project/references/parallel-execution.md),
which is the normative design. This document is the build order for it, and nothing more: where the
two disagree about what the protocol *is*, the reference wins; where they disagree about what to
build next, this document wins.

Six phases, each separated from the next by a review gate. The gate is not a formality — each phase is
allowed to conclude that the phase after it is not worth building, and Phase 6 is allowed to conclude
that concurrency itself is not worth enabling. §20 of the reference carries the same table; this
document is where the phases acquire enough detail to act on.

**Only Phase 1 is specified to the level someone can implement.** Phases 2 to 6 are a roadmap, stated
at the resolution the reference supports today, and each is expected to change once the phase before
it has been built and reviewed. That asymmetry is deliberate: five prior review rounds established
that the expensive failure mode of this design is not an unbuilt phase but an approved phase that
turns out to rest on an unchecked assumption. Building one small piece and reviewing it produces
information no further design round can.

Two rules apply across every phase.

- **No phase begins because its predecessor finished.** Each phase's exit gate is a review of that
  phase's own artefact against the section of the reference it claims to implement. Finishing Phase
  *n* authorizes proposing Phase *n+1*, not starting it.
- **A phase that contradicts the reference amends the reference.** If Phase 3 discovers that a record
  schema cannot be encoded as §7.4 describes, the fix is a design amendment with a disposition in
  [`parallel-execution-decisions.md`](parallel-execution-decisions.md) — not a shipped module that
  quietly differs from the document that is supposed to describe it.

| Phase | Deliverable | What stays disabled | Exit gate | Behaviour change |
|---|---|---|---|---|
| 1 | The claim representation and the conflict relation (§9.3) as a pure library, with its tests | everything else: no store, no locks, no CLI, no caller | the API and the conflict matrix reviewed against §9.3 | none |
| 2 | A one-host feasibility spike on a disposable project: can a separate agent be given a task, observed, and its scopes sealed (§16, §17)? | real projects, dispatch, any framework built on the answer | a real agent task demonstrated, with defensible lifetime and stop behaviour, or the boundary revised | none |
| 3 | The record schemas (§7.4), canonical serialization, digests (§11.2) and `publish_if_absent` (§7.2), with crash injection between durable effects | canonical mutation, task execution | identities, the immutable `assessment` record (§11.5), and real filesystem failure prefixes validated | none |
| 4 | The projection (§6.1, §6.3), the derived label (§6.2), the operations (§8) and recovery (§13) behind one guarded entry point, against a fake adapter and a test store | user-facing activation | successful, failed, stopped and interrupted runs all obeying the same operations, invariants asserted at the durable effects and not only at intent | none |
| 5 | `enable-execution` (§7.6), the v4 field, the reader-only refusal, and sequential integration at capacity one on a disposable project | concurrency | canonical commit, evidence, ownership and old-reader behaviour verified on every affected host | **yes** — a v4 project is unreadable by an installation without this phase, and activation is irreversible for the generation |
| 6 | An opt-in pilot at `max_concurrent` 2, plus the measurement harness of §3.2 and §21.3 | unsupported task shapes; unattended retry of an ambiguous start | real overlap shown safe, useful and measured against a sequential baseline | **yes** — concurrency |

## Phase 1 — the claim representation and the conflict relation

The one phase specified to implementable detail. It ships a pure library and nothing else: no store,
no locks, no CLI, no `SKILL.md` change, and no caller anywhere in the shipped code. After it lands,
running every existing command produces byte-identical results to running them before it landed.

The reason this is first is that §9.3 is the part of the design with the least dependence on anything
unresolved. It needs no host adapter, no durable store and no schema version; it is a total function
over declared claims, and it is the part every later phase consumes. If it is wrong, every phase built
on it inherits the error, and it is cheap to review in isolation exactly once.

### Files

```text
plugins/research/skills/project/scripts/execution_claims.py
tests/plugins/research/project/test_execution_claims.py
```

`tests/conftest.py` already inserts `plugins/research/skills/project/scripts` into `sys.path` and
exports `SKILL_SCRIPTS`, and the type-checker's extra path already covers that directory, so no path
configuration changes. The test module imports `execution_claims` directly, the way the existing
model and unit tests import `workspace_lib`.

### Public surface

Three functions and two immutable types. The names below are the ones to implement; review 05 offered
them as illustrative, and adopting them verbatim costs nothing and makes the review a comparison
against a fixed target rather than against a paraphrase.

```python
normalize_claims(claims) -> Tuple[Claim, ...]
claims_conflict(left: Claim, right: Claim) -> bool
find_conflicts(requested, held) -> Tuple[Conflict, ...]
```

`Claim` is an immutable typed record with exactly three fields:

| Field | Type | Meaning |
|---|---|---|
| `namespace` | `str` | `local` or `store`. `external` is refused (§9.2); nothing here maps a URL-like reference to a claim. |
| `key` | `str` | the **already-resolved comparison key**. For `local`, an absolute POSIX path in one canonical spelling. For `store`, an attempt id, opaque. |
| `access` | `str` | `read` or `write`. |

`Conflict` is an immutable typed record naming the pair that conflicted and why: the requested claim,
the held claim, and a short machine-readable reason (`same_key`, `ancestor`, `descendant`). The
reason exists so that a future admission step can say *which* declared resource is blocked, which is
the whole purpose of returning pairs rather than a boolean.

The design's on-disk claim is a string with the access mode carried elsewhere in prose; this type makes
the mode explicit at the one place the conflict relation reads it. **No on-disk schema changes** — the
reference's `claims[]` shape is untouched by this phase, and the mapping from a plan's declarations to
`Claim` values belongs to Phase 3, where the plan schema is settled.

### Implementation boundaries

1. **Comparison keys arrive resolved.** The module never calls `Path.resolve()`, never probes a volume
   for case sensitivity, never reads a symlink, never hashes a file, and never infers a resource from
   a command string. A future filesystem/plan adapter owns actual filesystem identity and case
   normalization. The module's docstring must say so in those terms, because the correctness this
   phase establishes is correctness *of comparisons over its inputs*, and §9.2's `[UNENFORCED U2]`
   symlink residual stays exactly as unenforced as the reference says it is.
2. **Validation is deliberate, and it is an error.** Reject: an unknown `namespace`; an unknown
   `access`; an empty `key`; a `local` key that is not absolute; any `local` key containing a `.` or
   `..` component, a trailing slash, or a repeated separator (one canonical spelling, never two
   spellings treated as two resources); a NUL in any field; a non-string field. Every rejection raises
   one module-defined exception type with a message naming the offending field and value, and every
   rejection listed here has its own test. A malformed input must not produce an `IndexError`, a
   `TypeError` from iteration, or a silent pass.
3. **`local` paths compare by component.** `/repo/a` is an ancestor of `/repo/a/b`; `/repo/a` is *not*
   an ancestor of `/repo/ab`. Two `read` claims never conflict. A `write` conflicts with an equal,
   ancestor or descendant claim of either access from another grant. The relation is symmetric: §9.3
   says revision 4's one-directional statement was wrong, and a test asserts symmetry over the whole
   matrix rather than trusting the implementation's shape.
4. **`store` keys are opaque and exact.** `store:<attempt-id>` conflicts only with the same attempt
   id; no ancestry is computed over an attempt id, because it is an identifier and not a path. Claims
   in different namespaces never conflict at this layer. `external` is refused at validation, per
   §9.2's third row — not silently accepted and then found never to conflict, which would be
   indistinguishable from an unenforced claim.
5. **Normalization is deterministic and minimal.** Duplicate identical `(namespace, key, access)`
   entries collapse to one; where the same `(namespace, key)` appears as both `read` and `write`, the
   `write` subsumes the `read`. Distinct parent and child declarations are both preserved — subsuming
   `/repo/a/b` into `/repo/a` is a separate optimization with its own tests, and this phase does not
   do it. Output order is a total, documented sort (`namespace`, then `key`, then `access`), so
   `normalize_claims` is idempotent and its output is reproducible across runs and interpreters. No
   trie, no interval tree: the supported capacity is four attempts.
6. **Grant boundaries live at the caller.** `find_conflicts(requested, held)` compares two claim
   sequences belonging to *different* grants. An attempt holding both a task write and a check read on
   one file is not a conflict (§9.3), and the module does not need to know that, because the caller
   never passes one grant's claims as both arguments. The docstring states this precondition; the
   module does not enforce it, and a test documents the intentionally meaningless result of violating
   it rather than pretending a guard exists.

### Required tests

Every row is a distinct test, and each is named for the property it establishes rather than for the
function it calls:

- read/read never conflicts; read/write conflicts; write/write conflicts
- equal keys, both directions
- ancestor in both directions (`/repo/a` vs `/repo/a/b`)
- prefix siblings do not conflict (`/repo/a` vs `/repo/ab`)
- distinct roots do not conflict
- `local` and `store` never conflict with each other
- equal `store` ids conflict; different `store` ids do not; no ancestry is applied to a `store` key
- every malformed input in boundary 2, one test each, asserting the exception type and that the
  message names the field
- duplicate normalization collapses, and write subsumes read
- distinct parent and child declarations both survive normalization
- `normalize_claims` output is a documented total order, and is idempotent
- `claims_conflict` is symmetric across the full matrix
- `find_conflicts` returns reproducible pairs whose reasons match the relation that fired
- case-insensitive aliasing is tested **as equal pre-normalized keys**, with an explicit comment that
  this module does not detect filesystem aliases and the test is not evidence that it does

### Constraints

- Standard library only, and Python 3.9-compatible: the shipped scripts run under the system
  interpreter (macOS 3.9.6), so no `match`, no `X | Y` annotations, no `tuple[...]` subscripting at
  runtime — use `typing.Tuple` and friends, as `workspace_lib.py` does.
- Public functions and the two record types are typed.
- Do not import the design's reference model (`tests/plugins/research/test_parallel_execution_model.py`)
  into shipped code, in either direction. The model is a design-stage artefact; the module is shipped.
- Do not refactor `workspace_lib.py` or the launchers in this PR.
- Ruff's configured line length is 120 and the target version is `py311` for lint purposes; the
  runtime floor is still 3.9 and is not expressed in that setting.

### Explicit non-goals

No registry directory, no locks, no durable writes, no capture schemas, no schema v4, no plan-text
interpretation, no CLI command, no `SKILL.md` activation, no host API, no background process, no
migration, no automatic retry, and no change to sequential execution. If the first PR appears to need
any of these, the PR is wrong and should be split, not widened.

### Definition of done

- The conflict API and its documented preconditions are understandable without reading the design
  reference. A reviewer who has never seen §9.3 should be able to review the matrix.
- Every new shipped statement is covered: `pyproject.toml` sets `fail_under = 100` over
  `plugins/research/skills/project/scripts`, so an uncovered branch in the new module fails the gate.
- `uv run pytest -q`, `uv run ruff check .`, `uv run ty check .` and `uv lock --check` all pass, and
  the report states their exact output.
- Python 3.9 compatibility is verified in an available runtime rather than inferred from the
  development toolchain's version.
- No runtime caller imports or enables the module, and running the existing commands changes no
  project file as a result of the PR.
- **Review the PR and stop.** Phase 2 is not implicit follow-on work.

No release bump is needed merely to submit an inactive implementation PR, unless repository release
policy calls for publishing it. Any actual plugin release uses the normal synchronized version bump
and host update process; editing this checkout never updates installed copies.

## Phase 2 — prove the delivery path before building the framework

Adjustable after Phase 1. Stated at the resolution the reference supports today.

One host, one disposable project, one bounded task that creates a new file. The question is whether a
task instruction reaches an actual separate agent, whether that agent's execution is distinguishable
from its verification, whether output and evidence come back, and whether stop and completion can be
interpreted under §6.1's scope rules. Identify the logical coordinator's lifetime and what a successor
can observe after it disappears.

This phase sits second on purpose. §17.1 concludes that on the host this design was written for, no
adapter can seal a subagent — so the entire asynchronous path rests on a host question that no amount
of prose settles. Answering it on a disposable project is cheaper than discovering it in Phase 6.

It is a feasibility experiment, not a general host adapter. Record what is *observed*, what is merely
a cooperative rule, and what remains unavailable. A shell-only command fixture does **not** satisfy
the separate-agent objective: a subprocess the coordinator spawns and waits on answers a different
question than an agent with its own context. Address R5-05 and R5-08 here. If the spike succeeds,
freeze the minimal adapter contract for later phases; if it fails, stop or revise that boundary only —
a failed spike is a finding about §17, not a reason to redesign §7.

## Phase 3 — real storage primitives, then schemas

Adjustable after Phase 1 and Phase 2.

Two reviewable changes if it helps: the byte-publication helper first, then typed record encoding and
validation. `publish_if_absent` (§7.2) with `link` as the no-clobber operation, the canonical
serialization and digests of §7.3 — `body_digest`, `content_digest` and the identifier derivations —
and the §7.4 record schemas with their two envelopes.

Settle R5-02 and R5-06 before declaring any persistent format stable. Use random or payload-derived
ids with no self-reference. Keep observations (`capture`) and assessments (`assessment`) separate, per
§11.5. Reject malformed arbitrary JSON without treating it as absence — §6.3 and the model's I5 are
the specification, and a stored `null` is a record that exists and cannot be read.

Test in temporary directories: create, identical, conflict; preservation of the original bytes on a
conflict; missing ancestors; short writes; file and directory sync failures; and retry after
publication but before acknowledgement. Keep `atomic_write_text` unchanged for its existing
replacement callers — this phase adds a helper beside it, it does not repurpose it. State the precise
durability boundary rather than claiming power-loss testing from injected Python exceptions; §7.5's D1
and D2 classes are the claim, and an injected exception tests the code path, not the hardware. No real
project activation, and defer garbage collection until the retention invariants are proven.

The phase backlog's Phase-3 rows (§20's small corrections, tabulated in review 05) are this phase's
design work: id validation before path construction, the project-level versus attempt-level envelope
split, an actual order rule for every identity-bearing array, log digests and draining and redaction
and the settings the text references, and the locked mutable-grant update that must not use the
immutable journal helper.

## Phase 4 — executable operations and recovery

Adjustable after Phase 3.

The projection (§6.1, §6.3), the derived label (§6.2) which gates nothing, the twenty operations of
§8.1, the crash-prefix completions of §8.2 and the recovery of §13 — all behind **one guarded entry
point**, against a fake adapter and a test store.

Resolve R5-01, R5-03, R5-04 and R5-05, and align the design's reference model with the exact operations
implemented rather than with a parallel prose model. Unique mutation ids, per-invocation scopes,
explicit stop intent, and every recovery action applied through the same guarded entry point the
forward path uses — that is the property the model established and the one the implementation has to
keep. A fake adapter controls delayed starts, unknown outcomes, child scopes, failed checks, late
results and termination attestations.

Exit only when tests cover complete successful and failed runs, all implemented durable prefixes,
crashes *during* recovery, two competing owner acquisitions, non-conflicting completions at different
revisions, and no dispatch after a stop. Invariants are checked at the actual durable effects,
including the canonical commit, not only at intent publication — the model's I3 was wrong at the
intent, and the same mistake in shipped code would be invisible. Fix R5-07's concrete probes rather
than increasing test counts.

## Phase 5 — explicit activation and sequential integration

Adjustable after Phase 4. **Behaviour-changing**, and the earlier of the two phases that is.

A dedicated migration path for `enable-execution` (§7.6). Changing `schema_version` is an exception to
the ordinary immutable-field rule, not an ordinary candidate update, and the activation text's "the
block is new" does not exempt the simultaneous version change. Preserve authorization state and
terminal history, write `project.json` last, retain post-commit index recovery, and handle the
config-written/state-not-committed prefix. Separate initial owner acquisition (O19) from takeover
(O17) — they are different operations with different fences (§12.3).

Then integrate the proven adapter at capacity one, on disposable projects first. Test complete
canonical candidates, receipt append and deduplication, active-attempt binding, graceful owner release,
reopening, and old-reader refusal on every affected host surface. Preserve existing unactivated v3
behaviour exactly. Do not activate a live project on an installation that can read v4 but cannot
execute or recover it safely.

This phase gets its own implementation review and an opt-in rollout. Do not ship schema migration as a
standalone user-visible feature before a working execution and recovery path exists behind it: a v4
project an installation cannot execute is a project that installation has made unreadable for nothing.

## Phase 6 — bounded parallel pilot and measurement

Adjustable after Phase 5. **Behaviour-changing**, and explicitly conditional.

Two independent new-file tasks, not arbitrary repository editing. Test actual overlap, conflicting
read/write exclusion, failed sibling handling, stop during execution, and recovery without a double
start or a double receipt import. Exercise two projects sharing one participating workspace.

Settle R5-09 here, and only here. Measure sequential against parallel from equivalent starting
states, with repeated samples, over linear, tiny-task and independent-analysis graphs. Report wall
time, coordinator and worker tokens, total cost, journal overhead and judged result quality. §3.2's
benchmark is not yet reproducible in this repository, and this phase is where it becomes so; the
`max_concurrent` 2 of the pilot is this phase's setting to test, never a capacity the measurement
already established.

Then decide: keep two, try four, or leave parallelism disabled. **If the measurement shows no win, the
correct outcome is to stop at Phase 5** with a protocol that runs sequentially, correctly, and refuses
visibly — a better result than concurrency nobody measured. Directory hashing, external effects,
worktrees, live-owner takeover and automatic retry of an ambiguous start remain separate future
proposals (§22), not Phase 6 scope.
