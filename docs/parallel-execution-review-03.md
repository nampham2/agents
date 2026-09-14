# Parallel execution design — review 03

Date: 2026-09-11

Reviewed branch: `parallel-execution-design`

Reviewed commit: `fd24db15b9e6053c87096f5c628ccc7ee2a77892`

Reviewed sources:

- [Normative reference at the reviewed commit][design].
- [Decision record at the reviewed commit][decisions].
- [New documentation checker at the reviewed commit][checker].
- Existing canonical-state, authorization, evidence, locking, and migration code.
- The original design at `e18e589` and both previous reviews.

Previous reviews: [review 01](parallel-execution-review.md) and
[review 02](parallel-execution-review-02.md).

All design line numbers below refer to the reviewed commit. This is a review of an unimplemented
execution protocol and its new documentation test, not a report of failures from a deployed executor.
Only this review file is added by the review task.

## Assessment

**Request changes before implementation.** Revision 3 is more concrete and fixes several important
problems: dispatch intent is explicit, canonical evidence identifies an accepted receipt, staging is
deferred, capture provenance has separate axes, and the documentation checker is committed.

The additional detail also exposes contradictions that were harder to see in the earlier prose.
The target lock fails open; coordinator fencing is separated from the commit it is meant to fence;
coarse repository claims do not conflict with ordinary path claims; input hashing can invalidate a
task for making its intended edit; and the capture schema cannot represent its own provenance model.
These require changes to the specified rules, not merely careful implementation.

The next iteration should focus on a smaller, executable model of admission, transitions, and
acceptance. Adding more statements that a property is enforced will not resolve conflicting rules.
Each principal finding below includes an example and an implementable correction.

Priority meanings:

- **P1:** A specified behavior can admit conflicting work, accept or lose the wrong record, or
  prevent ordinary authorized work from completing.
- **P2:** A material protocol, compatibility, or validation gap that needs resolving before its
  affected feature is enabled.
- Smaller editorial and consistency issues are collected separately.

## R3-01 — P1: A failed target claim still permits a competing writer

References: design §8.5, lines 719–737; §12.2, lines 897–902.

The coordinator takes a target claim for parallel execution, but failure to acquire it falls back to
capacity one. Capacity one constrains one project's worker count; it does not exclude another project.

```text
Project A acquires the target claim and launches two writers in /repo.
Project B targets /repo and fails to acquire that claim.
Project B falls back to its inline executor and writes into /repo anyway.
```

Both projects are using this protocol. The disclaimer about pre-existing sequential coordinators
does not cover this case. The fallback defeats the new lock's purpose.

The nested-root rule has two further problems:

1. Including every ancestor up to `/` makes unrelated targets share an ancestor claim. If those
   claims are compared using the specified overlap rule, `/repo-a` and `/repo-b` conflict through
   `/`. The protocol can serialize every project in the workspace.
2. The actual lock directory is keyed by the exact target root. `/repo` and `/repo/plugins` have
   different lock paths. Atomic creation of those separate directories does not serialize a
   scan-and-admit decision over nested roots. That requires a shared admission lock or equivalent
   atomic registry operation.

**Correction:** Every executor, including the inline executor, must acquire the target reservation
before accessing a conflicting target. If admission fails, wait or report the conflict; do not
execute around it. Under a short workspace-level registry mutex, compare the actual resolved roots
using equality or proper ancestry, then insert the reservation. Do not claim every ancestor as an
exclusive resource. Define release and stale-owner reconciliation for this registry too.

**Required tests:** Parallel versus inline on one target; simultaneous admission of nested targets;
simultaneous admission of disjoint targets; stale target ownership after coordinator interruption.

## R3-02 — P1: Ownership checking and canonical mutation have a fencing race

References: design §12.1, lines 879–889; §12.2, lines 904–910.

Every mutation checks ownership under `.store.lock`, but the store lock must be released before a
canonical commit. This allows a superseded coordinator to commit through the supported protocol:

```text
A checks generation 4 while holding the store lock.
A releases the store lock, as required before commit.
B takes over and writes generation 5 to runtime.json.
B has not changed project.json's revision.
A commits using the still-current canonical revision.
```

The revision check does not detect the ownership change. A second unlocked ownership check would
only move the race window.

The ownership operations also do not explain how `Acquire`, which creates an existing lock
directory, performs takeover. Removing an expired directory and then recreating it is not an atomic
ownership transfer. Target claims are acquired first and held for the run, but their stale-owner
protocol is unspecified, potentially preventing takeover from reaching the coordinator lock at all.

**Correction:** Define a shared serialization point for ownership transfer and canonical replacement.
One option is to hold the store lock through the short canonical validation/replacement phase, then
release both locks before `_rebuild_index_after_commit`. This requires splitting the existing commit
operation's critical phase from post-commit cache work; it does not require holding the store lock
through index rebuilding. Takeover must participate in the same fencing protocol.

Also fence the permission to start physical work. A coordinator that passed a check and was then
superseded must not release a delayed host launch without revalidating an attempt-specific start
permit. Merely fencing later `state.json` updates cannot stop a stale host call.

**Required tests:** Pause A between ownership validation and canonical replacement, transfer to B,
and verify A cannot commit. Repeat at the physical-work start boundary. Race two takeover attempts.

## R3-03 — P1: Coarse repository claims do not exclude ordinary path writers

References: design §8.2, lines 607–618; §8.3, lines 678–701.

`exclusive` resources compare by exact name, while `path` resources compare by ancestry. No rule
makes `exclusive:repo:/repo` conflict with `path:/repo/b.py`.

The required two-task test gives both tasks a repository-wide check, so both happen to request the
same exclusive key. It misses the asymmetric case:

```text
A: writes a.py and reserves exclusive:repo:/repo for the full test suite.
B: writes b.py and has a task-scoped check, so requests only path claims.
```

The stated comparison rules admit both. A's suite can observe B's edits even though the document
says repository-wide verification serializes against every writer.

There is also a self-conflict: a coordinator rerun takes its own claims while the attempt retains
the complete verification claims. Without an explicit same-attempt rule, the rerun conflicts with
the reservation already held on its behalf.

**Correction:** Connect the resource namespaces. For example, every operation within a repository
takes a shared repository guard in addition to its path claims; a repository-wide operation takes
that same guard exclusively. Alternatively, define the cross-namespace conflict rule directly.
Coordinator verification for an attempt should execute under that attempt's pre-reserved grant,
after its worker has stopped writing, rather than acquire a competing grant.

**Required tests:** Global check versus scoped writer; global check versus ordinary reader where
required; coordinator rerun under its own attempt's reservation; unrelated repositories.

## R3-04 — P1: Recomputing the contract can reject a task's intended edit

References: design §6.4, lines 465–490; §6.5, lines 509–514.

`input_identities` contains hashes of mutable inputs. Invalidation recomputes the contract and
compares it with the frozen hash. For normal maintenance work, an input is also an output:

```text
Prepare task to read and update config.py; input digest is H0.
Worker correctly updates config.py; its output digest is H1.
Coordinator recomputes the contract from current files.
The input digest is now H1, so the contract differs and the task is quarantined.
```

The design does not distinguish an immutable description of the initial input snapshot from a
continuously re-hashed live input. It also does not specify the ordering between reserving inputs
and capturing their hashes, leaving a preparation-time race.

**Correction:** Separate the planned execution definition, initial input snapshot, and produced
artifact identities. Recompute the definition to detect replanning, but preserve the initial
snapshot for read-modify-write inputs. Require stability of read-only inputs under reservations;
validate intended writable subjects against output and verification receipts. Reserve the complete
claim set before taking the baseline snapshot.

Specify how derivation changes are handled during takeover. A new reader should not silently
reinterpret an old dispatch with a new inference algorithm and then call the resulting difference
a task change.

**Required tests:** Editing an existing file succeeds; a read-only input changed by another actor
is detected; changing the task definition invalidates the attempt; snapshot acquisition races are
controlled.

## R3-05 — P1: T21 uses a nonexistent authorization status and omits the no-authorization case

References: design T21, line 417; §6.4, lines 492–496. Existing code:
`workspace_lib.py:29` and `_validate_authorization` at lines 514–553.

T21 quarantines a task whose authorization status is no longer `authorized`. The canonical enum is:

```text
not_required | pending | explicit | denied | deferred
```

`authorized` is not a valid value. A literal implementation quarantines valid `explicit` tasks and
the normal delegated tasks whose authorization is `not_required`.

There is a second interaction to resolve: current validation rejects a `RUNNING` authorization-required
task whose status is no longer `explicit`. The design says revoked in-flight work remains canonically
`RUNNING` while its attempt is quarantined. Updating canonical authorization to `denied` without a
compatible state transition would already fail validation.

**Correction:** Specify one authorization predicate shared with canonical validation: distinguish
`required: false`/`not_required` from `required: true`/`explicit`, and verify scope and current
authority separately. Define where a withdrawal is durably recorded while physical execution is
unresolved, and how that maps to the new canonical schema. Do not erase a withdrawal merely to keep
an otherwise incompatible `RUNNING` record valid.

**Required tests:** Valid read-only and local-write tasks with `not_required`; valid scoped
`explicit` authorization; withdrawal before dispatch, during work, and after work but before acceptance.

## R3-06 — P1: Quarantine is terminal for scheduling but may still contain a live writer

References: design §5.2, lines 291–293; §6.1, lines 375–388; T20/T25, lines 416–421;
§8.3, lines 668–670; §12.1, lines 884–885.

`QUARANTINED` is classified as terminal, yet it can be entered while the worker is running, holds
reservations, and requires a later transition. Admission and takeover consider non-terminal
attempts, and `runtime.active_attempts` explicitly excludes terminal ones. A dispatch pause partly
masks the inconsistency, but does not make the state safe to omit from ownership or resource accounting.

More directly, T25 releases reservations after an explicit dated decision. It does not require the
worker or its descendants to have stopped:

```text
Worker is quarantined because its contract changed.
Operator records a decision to retry.
T25 releases reservations and returns the task to TODO.
A replacement starts while the first worker can still write.
```

T17 only reconciles an uncertain attempt that never wrote anything. There is no complete path for
an uncertain worker that stopped after making partial edits. T23 routes cancelled work through that
same restrictive path.

**Correction:** Define quiescence and reservation ownership independently of successful completion.
Treat quarantine as unresolved/non-terminal while it holds claims. All held claims must participate
in admission and takeover regardless of the display state. Require proof of stopped execution plus
a recorded disposition of partial outputs before any reconciliation releases claims. Add a path
for stopped, partially executed work.

The late-result rule also needs revision: T18 checks whether a replacement reached `INTEGRATED`,
not whether a replacement exists or the old attempt was revoked. Normal admission forbids a
replacement while the old attempt is `UNCERTAIN`; after reconciliation the old attempt is no longer
in a state T18/T19 handle. Specify late arrivals against an explicit attempt-supersession record.

## R3-07 — P1: The transition table still has uncovered cross-file crash windows

References: design T1–T5, lines 397–401; T14/T17/T25; §13, lines 917–934.

The assertion that every interruption between durable writes is covered is not satisfied.

Examples:

| Crash window | Persisted state | Missing recovery decision |
|---|---|---|
| T1 after reservations but before `state.json` | Claims exist without a complete prepared attempt | Where reservation records live, how they are enumerated, and which record establishes ownership |
| T3 after canonical `RUNNING` commit but before `DISPATCHING` | Canonical task is `RUNNING`; attempt remains `PREPARED` | §13's `PREPARED` row applies only when the task was not committed `RUNNING` |
| T5 after `LAUNCH_FAILED` but before canonical `TODO` | Terminal attempt, canonical task still `RUNNING` | Takeover skips terminal attempts; the canonical transition is not ordered in the durable-write column |
| T14 after acknowledgement but before `INTEGRATED` | Canonical completion and ack exist, but reservations remain held | The recovery table specifically discusses missing acknowledgement, not incomplete post-ack finalization |

State is duplicated across `state.json`, `runtime.active_attempts`, `runtime.reservations`, and the
unnamed reservation records. The transition table mostly writes `state.json` and does not specify
updates or precedence for the other representations. An interrupted mutation can leave them disagreeing.

**Correction:** Choose an authoritative execution journal or state record and classify the other
representations as projections. Specify recovery from every prefix of each durable write sequence,
not just from selected state labels. Bind a canonical `RUNNING` transition to the exact attempt
being dispatched, so recovery does not infer attempt identity from task status alone. Finalization
must be idempotent whether neither, one, or both of acknowledgement and terminal state were written.

This is a good place for a small executable transition model: enumerate crash points mechanically,
reload only persisted values, and assert canonical coherence and retained ownership before dispatch.

## R3-08 — P1: Malformed publications and pending failures can evade the dispatch pause

References: design §5.3, lines 306–313; T9–T13, lines 405–409; §7.1, lines 539–545;
§14, lines 965–980.

The document assigns incompatible outcomes to the same input:

- Unknown schema majors and corrupt JSON must pause dispatch under §5.3.
- The completeness check says anything outside its exact success condition is T10 or T11.
- T10 treats missing or digest-mismatched captures as incomplete publication and rereads later.
- T13 and §14 treat malformed results, capture failures, and digest mismatches as quarantine/pause events.

Under manifest-last publication, a present manifest with missing or corrupted immutable dependencies
is not the normal pre-publication state. Treating it as routine waiting can conceal damage indefinitely.

The recovery scan also checks attempt states only. A worker can durably publish `outcome: failed`
while `state.json` remains `RUNNING`. If the coordinator crashes before recording the failure,
the next state-only scan sees neither `UNCERTAIN` nor `QUARANTINED` and may admit new work.

**Correction:** Define a precedence-ordered publication classifier: absent manifest, unsupported or
malformed record, invalid identity, invalid dependency/digest, valid failed/blocked result, valid
successful result. Specify one transition for each. Recovery must classify pending publications
as well as state records before admission. Resolve the ordering contradiction between §14's
pause-first rule and T11/T13/T20/T21's state-first writes.

**Required tests:** Crash after publishing a failed result but before the pause; unknown major;
wrong contract hash; missing capture after manifest publication; corrupt log digest; malformed JSON.

## R3-09 — P1: Verification inference can silently weaken the required check

References: design §6.5, lines 505–508.

Being parseable into an argument vector does not establish that a string is a command. For example,
the standard shell lexer can tokenize both of these without error:

```text
Inspect the rendered report for clipped labels
Review the argument independently
```

The stated rule can classify these as commands named `Inspect` and `Review`, before reaching the
intended review or inspection branches.

More seriously, a string needing shell composition may be declared `inspection` instead of rejected.
That permits a required `pytest -q && ruff check .` verification to become a non-executed assessment.
The execution contract would then faithfully hash a weaker check than the canonical task specified.

**Correction:** Resolve the verification method explicitly during planning/admission; parsing should
only validate an already selected command representation. Preserve the required semantics. Reject
unsupported composition with an actionable request to express a command sequence or an explicitly
authorized shell invocation. Never convert an executable requirement to inspection because parsing
is inconvenient.

Persist the coordinator's structured interpretation and compare it against the task's stated
requirement. The lexical parser version is useful provenance, not evidence that this interpretation
was correct.

## R3-10 — P1: The capture schema cannot represent the specified evidence model

References: design §5.2, lines 219–237; §11.1, lines 791–808; §11.4, lines 846–855.

The field `capture` is defined twice with incompatible meanings:

- In `capture.json`, it is the capture ID, unique within an attempt.
- In the provenance model, it must be `process-record` or `structured-assessment`.

A record cannot carry both meanings in the same JSON key. Giving every command the ID
`process-record` also violates the multiple-capture requirement.

The adequacy table unconditionally requires command identity, working directory equality, and a
zero exit status for success. Successful inspections and human reviews have no command or process
exit status. They therefore cannot satisfy the comparison as written without inventing process data.

**Correction:** Use distinct fields, for example `capture_id` and `record_kind`. Define tagged record
variants and method-specific validation. Command records require argv/cwd/process outcome;
assessment records require assessor identity, subject identities, criteria, and an explicit verdict.
Neither should fabricate the other's fields. Apply shared identity and subject checks to both.

The actor enum is also too coarse to establish independent review. A separate worker can be an
independent reviewer; the producing worker is not. Conversely, `human` by itself establishes no
independence. Record an actor identity and its relationship to the producing attempt. Reserve a
human-only requirement for contracts that actually require it.

## R3-11 — P2: Receipt timestamps and missing lookup rules still make retries non-idempotent

References: design §5.2, lines 256–281; §7.2–§7.3, lines 551–579.

Removing the future canonical revision from the receipt is correct. However, `accepted_at` remains
part of its content hash. Repeating acceptance after a crash can generate a new timestamp and a new
receipt ID for the same result.

There is no `receipt_id` in `state.json` and no specified unique mapping from result identity to its
already prepared receipt. A crash after receipt publication but before marking the attempt
`VERIFIED` therefore leaves recovery without a defined way to reuse that decision.

The acknowledgement has a related precision issue. After a commit succeeded but before an ack was
written, later canonical revisions may exist. The revision observed on restart proves that the
receipt is present by that revision; it does not prove which revision first accepted it.

**Correction:** Persist and reuse a unique acceptance decision for an attempt/result pair, including
its timestamp. Make receipt discovery deterministic and reject conflicting prepared decisions.
Either persist the original accepting revision through a recoverable commit protocol, or rename
the acknowledgement field to an observed revision and stop claiming it identifies the original commit.
Always inspect the canonical evidence reference before retrying a commit merely because an ack is absent.

## R3-12 — P2: The schema-version gate requires a migration and reader rollout design

References: design §5.3, lines 316–322; §19, lines 1130–1133. Existing code:
`workspace_lib.py:3114–3115`, `3164`, `3204`, `3217`, and `3581` onward.

Failing closed for older installations is the right direction. It is not just a marker that the
existing commit path can write:

- `schema_version` is an immutable project field.
- Transactional commit currently requires schema v3.
- Validation, graph loading, allocation, and migration have version-specific behavior.

The design names no new canonical version, migration operation, activation ordering, or behavior for
reopening a project that has enabled execution previously. Adding the version gate in the final
activation stage cannot substitute for making the new state readable and writable first.

**Correction:** Specify the new canonical version and an explicit migration path under the project
lock. Preserve terminal history and authorization. Define interrupted activation recovery and make
all affected readers understand the new version before enabling execution. Include mixed-installed-
version tests for all three hosts. Do not describe the overall protocol as requiring no schema
change merely because timing stays in sidecar records.

## R3-13 — P2: Multi-check verification, failures, and coordinator reruns are not represented coherently

References: design §5.1, lines 168–177; §6.4, lines 460–462; §7.3; §11.2–§11.4.

The layout preserves multiple captures, but the contract has one `verification_method` and one
`verification_argv`. It cannot state that two different commands are both required, how their coverage
combines, or which retry supersedes a failed capture. Requiring every capture to equal the same argv
would reject a legitimate second check.

The receipt copies `captures` from the worker result. A coordinator rerun happens after that result
is published and immutable, so it cannot be added to the worker's manifest. The store ownership rule
also assigns the entire captures subtree to one worker or its helper, leaving ownership of later
coordinator-generated captures ambiguous.

**Correction:** Give required checks stable IDs in the contract. A capture names its check ID and
its execution sequence. The coordinator selects the qualifying capture for each required check,
preserves the failed history, and builds the receipt from that explicit selection. Keep worker
submission immutable while allowing separately owned coordinator captures to be referenced by the
acceptance decision. Define whether a failed record can still be integrated as evidence without
satisfying a successful task outcome.

## R3-14 — P2: Publication immutability needs a publish-if-absent operation, not ordinary replacement

References: design §5.1, lines 179–181; §7.1, lines 534–538; decisions, lines 123–128.

The protocol now requires a differing republication to be rejected without applying it, but still
describes every publication as using atomic replacement. A check for an existing file followed by
`os.replace` is not an atomic publish-if-absent operation.

If two invocations for the same attempt overlap, both can observe absence and the second replace the
first. If only the coordinator detects a difference after observing both versions, the first result
may already have been overwritten. There is no immutable first version to retain unless publication
actually enforces it.

**Correction:** Specify the publisher's serialization or no-clobber primitive. On a duplicate, compare
against the original immutable bytes and report conflict without replacing them. This may reuse
existing atomic-write internals, but the additional behavior must exist somewhere. Also specify
exact serialization for receipt hashes and repeated result publication; timestamps must be reused
rather than regenerated during a retry.

## R3-15 — P2: Worktrees are marked available without an integration protocol

References: design §9, lines 743–756; §6.4, lines 465–470; §19, lines 1125–1134.

Staging is correctly deferred because it requires promotion and confinement rules. Worktrees are
nevertheless marked “Available, per project” even though their merge/promotion rules are also absent.
The contract binds the canonical working directory and resolved output roots, while a worktree
executes and verifies in a different directory. The adequacy comparison requires those directories
to match.

There is no rule for dirty baseline changes, output-to-target mapping, merge conflicts, stale target
revisions, post-merge verification, or interruption during integration.

**Correction:** Mark worktrees deferred too, or specify a separate execution-root mapping and an
integration protocol before advertising support. The first release can use the two modes already
identified as shipping first. Do not let a table label enable a mode absent from the implementation
stages and acceptance model.

## R3-16 — P2: The decision history describes a different revision 1 and a different review 01

References: decisions lines 18–26, 50–55, 75–83; checker lines 3–5.

The new history says revision 1 had a workspace-root SQLite queue, wave-synchronous scheduling,
a 2–4x speedup claim, and no cross-process locking. These claims contradict the actual committed
revision 1 at `e18e589`:

| New historical claim | What the committed revision 1 says |
|---|---|
| Queue under the workspace root | One database per project, `<project-dir>/queue.db`, §5 |
| Wave-synchronous primary scheduler | A ready queue is primary; waves are fallback, §7.4 |
| Central 2–4x speedup claim | The 31-project measurements and 1.45x modeled mean already appear in §2 |
| No existing cross-process locking | `DirectoryLock` and revision-checked commits are explicitly listed in §3 |

The review-01 disposition table is also not a disposition of the linked review. The original review
has nine numbered findings, beginning with unreconstructible state/lossy drain, lease expiry, and the
start protocol. The new table substitutes a different six-finding list and drops other actual findings.

The real lock correction concerned the scope of the existing locks and the unsupported contention
argument. It was not a discovery that cross-process locking existed. The new test module repeats
the incorrect historical assertion in its docstring.

**Correction:** Reconstruct the history from the named commits and the checked-in review files.
If an earlier uncommitted draft is being described, give it a separate identity and cite an available
artifact rather than attributing it to revision 1. Preserve all nine original finding IDs with their
actual subjects. Remove unsupported claims about why the historical author made a mistake.

## R3-17 — P2: The documentation checker does not check several structural guarantees it advertises

References: checker lines 203–230, 242–261, 321–327; design §20.1 and §21.

The new checker is a useful addition and passes. Its mutation tests establish that selected changes
can trigger each check; they do not establish that the checks validate all the described relationships.

During this review, these three in-memory mutations each produced **no findings from `run_checks`**:

| Mutation | Structural gap |
|---|---|
| Delete the C1 citation row while retaining `[C1]` uses | Citation-use/table referential integrity is not checked |
| Change T14's canonical transition from `RUNNING → DONE` to `DONE → TODO` | Canonical transition validity is not checked |
| Change the companion path to `../../../../../missing/parallel-execution-decisions.md` | Cross-link checking only looks for the basename |

The transition checker checks state-name presence and an outgoing-row regex, not destinations,
guards, release behavior, or coverage of durable-write prefixes. Its `NON_TERMINAL_STATES` includes
`QUARANTINED`, contradicting the design's terminal list. The design says there are nine states while
the checker enumerates twelve, and both pass together.

**Correction:** Keep literal-presence checks narrowly labeled. Add checks for citation references and
duplicate IDs, actual Markdown link resolution, section references, canonical status enums, and
consistent state classifications. Use a structured transition fixture or executable model for
protocol invariants. Do not try to prove semantic correctness by adding more required phrases.

Some checks also need a planned retirement path: requiring “not yet reproducible in this repository”
must change when the benchmark actually ships. Otherwise CI protects a temporary disclaimer after
it becomes false.

## Smaller and additional consistency issues

These are separate from the principal failure traces. They should be corrected as part of the next
revision, without expanding every one into a new subsystem.

| ID | Location | Issue and correction |
|---|---|---|
| S01 | Design line 376; checker lines 126–151 | There are twelve named attempt states, not nine. Decide whether quarantine is terminal; make the diagram, table, runtime accounting, and test constants agree. |
| S02 | Design line 1192; decisions review-02 disposition | `§14.4` does not exist. The intended reference appears to be step 4 of §14, not a subsection. An independent heading check found this unresolved reference. |
| S03 | Design line 626 | The old instruction that `external` references become absolute paths survives despite the new namespace table explicitly saying otherwise. Remove it from the path-only rule. |
| S04 | Design lines 562–565 | Receipt validation is assigned to Stage 3, but the revised stages assign durable handoff and receipt validation to Stage 4. |
| S05 | Design lines 185–186, 213 | `handle` is not marked optional, yet its meaning depends on absence being distinguishable from null. Mark it optional and define the allowed values per state. |
| S06 | Design lines 202, 217, 290 | Nested deadline/pause objects lack complete requiredness and numeric bounds. `unresolved_reason` is marked optional but described as present in particular states. Define conditional requirements; an omitted heartbeat interval should have a precise meaning. |
| S07 | Design lines 475–480 | “Shortest round-trip” numbers is not a complete cross-writer canonical JSON specification. Define the supported numeric domain, reject non-finite values and duplicate keys, specify negative zero and Unicode handling, and apply one encoding to all content-addressed records. Avoid arbitrary numeric values where integers or strings suffice. |
| S08 | Design §5.2 and §6.4 | IDs, hashes, arrays, `adequacy`, `derivation`, resolved outputs, and input identities lack several field-level constraints. Specify path-safe ID syntax, digest format, uniqueness, resource element types, and method-dependent fields. Otherwise IDs can accidentally escape the intended store layout. |
| S09 | Design lines 251–252, 470, 529 | Hashing directories and external artifacts is undefined. Existing output references may name directories or durable external identifiers. Define tree manifests or restrict supported artifact kinds in the first release. A URL or receipt ID is not inherently a local byte stream to hash. |
| S10 | Design §6.5 | Input identities are said to be derived, but no derivation rule identifies the inputs. Resource derivation consumes that missing input set. State the coordinator's explicit declaration step and conservative fallback rather than implying the verification argv reveals all reads. |
| S11 | Design §8.2 | Resolving symlinks once does not protect a symlink or ancestor from later retargeting. Hard-link aliases also remain distinct paths. Define the supported alias model, revalidation point, or conservative exclusion policy. |
| S12 | Design lines 628, 637 | “Record both forms” has no corresponding unresolved-path field, and `runtime.json` has no documented case-sensitivity probe field. The schema tables should describe the records the comparison rules require. |
| S13 | Design lines 635–638 | One case-sensitivity result for the runtime is insufficient when target and workspace are on different volumes. Scope probes to the relevant filesystem; Python-style case-folding alone is not a specification of every filesystem's filename equivalence. C13 cites host parity, not supported operating systems or filesystem semantics. |
| S14 | Design lines 643–652 | `PROTECTED` mixes fully rooted entries with bare names. Make every root explicit. Parent write claims must also be rejected when they contain protected descendants. Distinguish worker grants from coordinator bookkeeping and legitimate coordinator-owned tasks that edit shared records. |
| S15 | Design §8.5 | Target reservations do not protect shared `workspace_root` outputs when two projects have different targets. Either reserve those shared paths in a common registry or prohibit their concurrent mutation under this protocol. Scope guarantees to one workspace root; unrelated roots do not share this registry. |
| S16 | Design T2/T3/T5 | Admission allows explicit retries from `BLOCKED`, but these rows assume the task began `TODO`. Specify normalization to `TODO` or transitions that preserve the actual previous status and block reason. |
| S17 | Design T20/T23/T24 | Contract invalidation omits `PREPARED`, `DISPATCHING`, and `VERIFIED`; cancellation omits `DISPATCHING`. A valid result from cancelled work may be recorded, but cancellation must still prohibit further side effects and new required checks unless authorized. |
| S18 | Design T21 and §14 | Authorization withdrawal quarantines work but the general rule lets in-flight work continue. Separate ordinary execution failure from a user instruction to stop or revoked authority. Make a best effort to stop further actions and report what cannot be stopped. |
| S19 | Design lines 236, 852–860 | Subject hashes taken only after a command do not establish that the subject stayed unchanged while the command examined it. Define stable verification subjects or permitted verification mutations. Do not claim this rejects every passing-check-then-modification sequence, including mutations inside the checking command. |
| S20 | Design §11.4 | The contract has no explicit per-check subject-coverage or independent-review requirement field, although adequacy compares both. Define the required artifact/check mapping and review requirement, including how omitted required outputs are rejected. |
| S21 | Design §17 | The `spawn` return type omits ambiguous failure even though its prose routes that case to T6. `discover` and `launch_key` placement are not specified as callable input/output contracts. Treat unknown/running/never-started as distinct discovery results. |
| S22 | Design §17 | Inline execution is promised equivalent recovery, but its handle, dispatch events, ownership of captures, and restart observations are not defined. Losing `observe` does not make inline subprocess descendants observable. Declare the actual guarantees of the inline adapter. |
| S23 | Design lines 950–953, 1088 | An “explicit resource check” can detect some surviving writers, but absence of a currently visible write is not proof that no process will write later. Specify acceptable termination evidence; otherwise retain uncertainty. |
| S24 | Design §5.5 | Current validation checks direct evidence files, not the transitive capture/log references inside receipts. The claim that removing any named capture already makes `research-validate` fail is unsupported by C22. Add graph validation explicitly. |
| S25 | Design lines 347–355 | Collection needs a durable tombstone before deletion, a crash protocol, and a policy for reopening closed projects. Define which record proves a missing file was intentionally collected. Also distinguish removing an empty lock directory during normal operation from forbidden collection under `execution/`. |
| S26 | Design §5.1 and §19 | Raw retained logs need size limits, redaction/secret-handling rules consistent with the existing skill, and a defined relationship between redacted bytes and hashes. Logging every byte forever should not be an implicit consequence of durability. |
| S27 | Design lines 12–13 | The companion link works in this repository but escapes the installed plugin subtree; `docs/` is not included in the nested plugin. Before activation, use an appropriate repository link or keep required runtime guidance inside the shipped package. |
| S28 | Design lines 1244–1245 | C24 points to the evidence-append helper rather than the launch/timeout exception handling it is cited for. C25 points to this repository's `TimeoutExpired` handler, not Python's documentation about descendant behavior. Literal needle matches do not support those attributed claims. |
| S29 | Design §19 and decisions alternatives | The reassessment stage refers to recorded database/MCP adoption conditions, but several conditions from revision 2 were dropped from the new decision record. Restore the relevant conditions or stop claiming they are preserved. |
| S30 | Design §1 versus §17 | “A worker's tool output never enters the coordinator's context” is unconditional in the objective discussion, but §17 permits hosts without bounded worker context. Qualify the former statement by capability. |
| S31 | Design §8.4 and §16 | Define whether the ceiling counts executing workers, all non-terminal attempts, or held reservations. Avoid literal claims of five-wide admission when the configured ceiling is four; describe graph width separately from actual worker count. |

## Recommended next revision and validation

The most useful next change is a compact executable protocol model with a small set of realistic
fixtures. The reference should be generated from or checked against the same state and record
definitions wherever practical. Field tables can remain prose, but they must describe a model that
can actually represent a successful run, a failed run, and recovery.

Prioritize these scenarios:

1. Edit an existing file, verify it, and accept it without changing its initial input identity.
2. Run an inspection-only task and a task requiring two different command checks.
3. Admit a scoped writer against an already reserved repository-wide check.
4. Reject a competing inline project when the target is owned; allow unrelated targets.
5. Interrupt every durable write prefix in T1, T3, T5, T12, T14, T17, and T25.
6. Transfer coordinator ownership between its last check and canonical replacement.
7. Restart with a published failed result but no recorded pause.
8. Reconcile a quarantined or uncertain attempt that wrote partial outputs while ensuring no old
   writer survives when its reservations are released.
9. Retry receipt creation and commit without changing the receipt identity or duplicating evidence.
10. Upgrade a v3 project, interrupt activation, and resume with both compatible and old installations.

These tests should assert persisted state, actual admission decisions, and whether a second writer
could start. Counting transition rows or checking that the word “recovery” appears is useful only
as a separate documentation-maintenance check.

Keep the first implementation's mode set small: read-only workers and cooperative shared-target
tasks with explicit claims. Worktree integration, confinement, and automatic retry can remain
deferred until their own protocols are specified and tested.

## Checks performed and limits

The following commands completed successfully against the reviewed checkout:

```text
.venv/bin/python -B -m pytest -q --no-cov -p no:cacheprovider -o log_cli=false \
    tests/plugins/research/test_parallel_execution_doc.py
    → 4 passed, 13 subtests passed

.venv/bin/ruff check --no-cache .
    → All checks passed

.venv/bin/ty check .
    → All checks passed
```

Additional read-only checks:

- The committed checker parsed 26 citation rows and accepted their literal ranges.
- An independent heading check found the unresolved `§14.4` reference.
- The checker enumerates twelve states and the reference has 25 transition rows.
- The three in-memory mutations described in R3-17 each passed `run_checks` without findings.
- Standard shell tokenization accepted the two natural-language verification examples in R3-09,
  demonstrating why lexical parseability alone cannot determine verification method.
- `git show e18e589:.../parallel-execution.md` contradicted the historical claims listed in R3-16.

No production executor exists in the branch. The concurrency and crash examples in this review are
reasoned traces through the specification; they are not claims that a nonexistent executor was
executed. The full Python regression/coverage suite and live host adapters were not exercised for
this pass. The successful lint, type, and documentation checks do not establish protocol correctness.

[design]: https://github.com/nampham2/agents/blob/fd24db15b9e6053c87096f5c628ccc7ee2a77892/plugins/research/skills/project/references/parallel-execution.md
[decisions]: https://github.com/nampham2/agents/blob/fd24db15b9e6053c87096f5c628ccc7ee2a77892/docs/parallel-execution-decisions.md
[checker]: https://github.com/nampham2/agents/blob/fd24db15b9e6053c87096f5c628ccc7ee2a77892/tests/plugins/research/test_parallel_execution_doc.py
