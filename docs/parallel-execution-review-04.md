# Parallel execution design — review 04

Reviewed on 2026-09-11 against commit `812010cbaf0ed6e140fcb282dde247a59641e7a0`
(design revision 4). The worktree was clean at the start of the review.

Scope: the entire [normative design](../plugins/research/skills/project/references/parallel-execution.md),
[decision record](parallel-execution-decisions.md), both design test modules, relevant shipped
validators/transaction helpers, and previous revisions where a historical claim needed checking.
Line numbers below refer to that commit, not to a future edited document.

## Assessment

**Not ready to implement as a settled protocol.** There are useful improvements: one path-conflict
relation, separate definition and baseline identities, publish-if-absent, a concrete schema-version
choice, tagged captures, and tests that actually exercise some decision functions. Deferring worktrees
and copies is appropriate. These changes should be retained.

However, several safety arguments still stop one step too early. A reservation protects against a
late launch only while it remains held; “never started” does not prevent a delayed caller starting
later. A published result is not a termination barrier. A valid-looking capture is not necessarily
evidence for the bytes being accepted. Most importantly, the new model does not model the external
execution and canonical state needed to check these properties. Some of its strongest-sounding tests
are assertions about labels or constant-return functions, not about the advertised invariant.

This review contains 18 principal findings and a smaller-detail checklist. P1 means a safety,
acceptance, or ordinary-workflow correctness issue to settle before executor implementation; P2 means
an important specification, validation, or implementation-quality issue. Failure traces are deductions
from the specified rules, not claims of production incidents: no production executor exists yet.

## R4-01 — P1: A delayed physical launch can outlive the reservation protecting it

**Locations:** design lines 1304–1318, 1378–1395; §12.3 and §13.2.

The acknowledged read-to-spawn race is not bounded to “wasted work” by the current rules:

1. Coordinator A writes the permit and reads generation 4, then pauses immediately before `spawn`.
2. B takes over at generation 5. A's grant remains held initially, as required.
3. Discovery reports `never_started`: this is a truthful answer at this moment.
4. B writes stop evidence, disposes of the attempt, releases its grant, and starts replacement C.
5. A resumes its already-authorized control path and calls `spawn` without another read.

Now A's worker and C can write the same subject. Rejecting A's eventual canonical commit does not undo
either filesystem write. An operator attesting that no worker exists has the same problem unless the
check also rules out a predecessor still capable of launching one.

**Required change:** distinguish “no execution exists now” from “this launch can never occur again.”
Release after an ambiguous start requires revocation/consumption of the start capability, or proof
that every process/session capable of issuing that start is permanently stopped. An adapter's
`never_started` response must not be sufficient by itself unless its contract includes a durable
launch-key tombstone rejecting all future starts with that key.

For an initial implementation, the simplest safe restriction is no automatic takeover/retry of a
possibly-live dispatcher. Keep its grant until the old dispatcher and its execution scope have been
terminated. Alternatively, design a small launch authority that serializes permit consumption and
revocation. Merely adding a second read moves the race; it does not close it.

**Test:** interleave two coordinators at the exact read/call boundary, obtain `never_started`, attempt
release and replacement, then resume the old caller. Assert at most one executable write authority.

## R4-02 — P1: Canonical fencing does not fence journal writes or coordinator checks

**Locations:** design lines 1271–1327, 1191–1261; decisions R3-02.

Moving the generation into `project.json` usefully makes takeover consume a revision. It does not
make every subsequent operation a fenced transaction. In particular, A and successor B can both run
coordinator checks under the same attempt grant. Own-grant conflict exclusion deliberately does not
serialize them. B can finish its check and release the grant while A's subprocess is still executing.
The next worker can then overlap A's verification or its incidental writes.

Likewise, A can publish a classification, acceptance, disposition, or release after losing ownership.
Publish-if-absent protects bytes against replacement, not against a stale writer winning the first
publication. There is no generation precondition on these journal mutations. A stale receipt can
become the immutable object the current owner is told to reuse.

The canonical argument also needs a precise command contract: ordinary `--expected-revision` checks
the revision supplied by the caller, not the caller's run identity. A stale coordinator loading a fresh
revision is not mechanically rejected by that argument alone. The stated stop-on-conflict rule is a
cooperative rule, not the stronger enforcement claimed in §12.2.

**Required change:** require `expected_run` and `expected_generation` on execution-related canonical
mutations and compare them under `.project.lock`. Define serialization/fencing for each coordinator
journal operation. Treat coordinator check executions as tracked execution scopes whose unresolved
presence prevents release; they cannot be invisible work under someone else's grant.

Also settle the lock lifecycle. `DirectoryLock` has a pid/creation record, no session heartbeat or
safe stale-owner replacement operation (`workspace_lib.py:319–363`). §12.1 says acquire the lock **or**
find an unresponsive owner, but never specifies how competing takeovers become the unique lock holder.
§12.3 prohibits holding any lock across a canonical commit, while listing the project commit lock and
a coordinator ownership lock. Define the actual acquisition/release intervals rather than a nominal
order. Preserve post-commit index rebuilding outside the project lock.

**Tests:** stale first publication; two simultaneous takeovers; an old owner exiting after lock
replacement; old/new coordinator checks overlapping; fresh revision with stale run identity.

## R4-03 — P1: “Stopped” and the inline fallback do not establish absence of future writes

**Locations:** design lines 960–965, 1245–1247, 1381–1395, 1540–1545.

The design treats `result.json` publication as “worker known stopped,” but publication can happen
while the worker is still running. There is no explicit worker rule prohibiting all later output
writes, background children, or resumption of the same host agent. `adapter_finished` and a dead
recorded process similarly say nothing about an untracked descendant's execution scope.

The inline argument is independently incorrect. A synchronous function may never *return*
`ambiguous`, but its caller can die while it is running. A canonical permit followed by an external
effect and a crash before result publication is still ambiguous. On restart, “discover always returns
finished after the call returns” says nothing about the call that never returned. Synchronous return
of `started(handle)` also contradicts the operation's promise that a worker is currently running.

Python documents timeout termination of the child process; it does not supply this protocol's
stronger execution-scope guarantee. Process-tree or detached-work behavior must be specified and
tested separately. [Python subprocess documentation](https://docs.python.org/3.9/library/subprocess.html#subprocess.run).

**Required change:** define a sealed-output barrier separately from process completion. Publication
must prohibit further output mutation and resumption under that attempt. Before releasing claims,
establish quiescence of every permitted executor, including check subprocesses. Specify whether
detached children are prohibited, supervised, or an operator-reconciliation case.

Keep inline on the same conservative interrupted-start path. External-effect attempts need durable
operation identity and effect-specific reconciliation, not blind replay. A host without the required
observation capability can retain uncertainty; it cannot claim uncertainty is impossible.

**Tests:** caller dies during synchronous execution; child survives parent; worker publishes then
continues; completed host handle resumes after release; timeout with remaining descendants.

## R4-04 — P1: Acquisition has an orphan prefix, and release has two opposite orders

**Locations:** design lines 222–235, 574–592, 740–749, 1013–1034, 1337–1349.

Acquisition durably inserts a grant and only afterwards writes `prepared`. A crash between them leaves
a grant whose attempt has no preparation, contract, baseline, or release record. The attempt is
unreadable by §6.1; `holds_claims` says false without `prepared`; the registry still excludes other
writers. §6.3 omits this prefix and the stale-grant rule forbids reclaiming it. A baseline-read error
after insertion produces the same state without a crash.

Release is contradictory:

- §8.5: write `release.json` **first**, then remove the grant.
- §5.1, §5.2 and T16/T24: release records that the grant **was removed**.
- §7.2: release the grant, **then** write `release.json`.

The latter order can lose the only registry exclusion before durable release evidence exists. The
former is safer but means `release` authorizes removal, not that removal already happened. Even then,
the journal's claim projection and the actual registry can temporarily disagree, contrary to §4.

**Required change:** choose one authority per question. The registry is admission authority, including
unprepared grants; the journal records durable release authorization. Add an explicit reservation
phase containing enough identity to reconcile grant-without-prepared. Handle the old preparing
coordinator's possible resumption using R4-01/R4-02, not absence alone. Document both acquisition and
release crash prefixes, including failed baseline derivation and directory-sync errors.

**Tests:** crash after grant insert; derivation fails after insert; release record present but grant
remains; grant missing without release; matching and mismatching project/attempt identities.

## R4-05 — P1: The label graph excludes real events and omits necessary transition guards

**Locations:** design §6.1–§6.3; model lines 168–223, 636–675.

Several ordinary executions do not fit the claimed transition graph:

- **Prelaunch cancellation/invalidation:** T25–T27 can quarantine a `PREPARED` attempt whose task is
  `TODO`. All dispositions T20–T23 assume canonical `RUNNING`. §6.3 separately says “release” a
  preparation, while §6.1 explicitly declares direct prepared-to-release unreachable.
- **Quarantine termination:** §13.2 requires stop evidence before disposition, but the only
  stop-evidence row T19 starts at `UNCERTAIN`, not `QUARANTINED`. The table has no quarantine-to-
  uncertainty route either. Following the prose gets stuck; following the unguarded graph releases
  a possibly-live writer. The model actually admits `hold → disposition(retry) → release` with no
  stop evidence.
- **Fast worker:** it can publish a heartbeat/result before the spawning caller writes `launch`.
  T6/T7 exclude `DISPATCHING`. Presence-derived labels can then jump straight to `PUBLISHED`, and
  the later launch write has no permitted source. This is especially unavoidable with inline work.
- **Late result:** after timeout/uncertainty, the worker can still publish. T7 excludes `UNCERTAIN`.
  Adding stop evidence then yields `STOPPED`, from which the only dispositions block, skip, or retry;
  there is no route to accept a complete successful late result.
- **Late human review:** T14 creates an immutable hold. A human assessment arriving afterwards cannot
  take the attempt back to adequacy/acceptance. All dispositions leave it non-DONE even if the actual
  work and review are now complete.
- **Supersession:** T22 requires release before preparing the successor. The old attempt is then
  `ABANDONED`, which T28 forbids as a source. T22/T28 therefore cannot be the advertised pair.

**Required change:** model asynchronous events independently of display labels. Distinguish
execution authority, canonical status, publication status, unresolved causes, and release permission.
Give each operation a precondition over those facts and an exact canonical effect. A display label
may summarize them, but should not prevent recording a real event.

Holds need individually identifiable resolutions, not one immutable disposition implicitly clearing
every current and future cause. Missing human review should be a waitable condition with a legal
resume path. Prelaunch disposal should preserve `TODO` without manufacturing a `RUNNING` transition.

**Tests:** each trace above, with actual canonical status and stop evidence in model state. Include a
second hold arriving after an earlier cause was resolved.

## R4-06 — P1: Failure classification clears the dispatch gate too early

**Locations:** design lines 777–796, 1352–1362, 1433–1444; model lines 316–335, 860–863.

`dispatch_blocked` has no clause for a classified-but-unintegrated failure. T8 writes classification
and immediately clears its first clause, even though the failed task is still `RUNNING`, no adequate
failure evidence has been selected, and no disposition exists. §13.1 claims the gate clears after
integration, which is false. The model explicitly tests that classification clears it.

There is also no consecutive-failure term in the supposedly complete gate. After three disposed
holds, the predicate admits again. Its record-set representation does not contain acceptance/hold
ordering or an explicit reset decision sufficient to define “consecutive.” Stop-request holds are
said both to count as attempts reaching a hold and to count toward nothing.

**Required change:** specify whether failures pause until classification, durable integration, or an
operator decision; then implement that exact policy. If the intended boundary is integration, include
classified failures without a committed receipt/resolution. Persist the failure-budget decision or
derive it from ordered decision records with a defined reset event. Do not use “all conditions” for a
predicate with extra undocumented gates elsewhere.

**Test:** publish failure, classify, crash, restart, attempt unrelated dispatch before integration.
Also three resolved failures with no successful acceptance, and a stop request between failures.

## R4-07 — P1: Adequacy no longer binds successful evidence to accepted output bytes

**Locations:** design lines 816–818, 1159–1187, 1207–1238; model lines 992–1045.

The adequacy table checks that subject *paths* appear before a check, not that the before/after
digests agree, match `produced`, or match the bytes being accepted. A worker can legitimately run a
check on version A, make a later edit to version B, and publish B with the passing capture for A.
All listed comparisons can pass. Merely recording both digests does not enforce their relationship.

The table also does not require consistency between `verdict` and actual process exit status.
`exit_status: exited(1)` plus `verdict: pass` is not rejected by any stated rule. The final choice reads
the capture's verdict, so the claim that the coordinator reads results “not claims” is too strong.

The failure rules disagree: lines 816–818 say a failing capture is inadequate and quarantined;
§11.5 says failure is adequate and BLOCKED; T15 allows BLOCKED only for `valid_failed`, though §11.5
requires it for a worker claiming success with a failed qualifying capture. Conversely, a
`valid_failed` outcome with later passing reruns is always BLOCKED by one rule but DONE by the model,
which has no result-outcome input at all. Refusal before checks also has no complete-capture path.

**Required change:** separate structural validity, artifact binding, and success evaluation. For each
check require a canonical subject snapshot identity, validate the after-snapshot against the accepted
snapshot, and enforce the mutation policy. Derive command pass/fail from an explicit expected-exit
policy rather than a free verdict. Define a truth table for manifest outcome, failed/missing/not-run
checks, and reruns. Record honest early failures without inventing check executions.

Inspection/review independence must compare producer and assessor identities: an inline coordinator
is itself the producer, so §11.6 cannot always stamp `relation_to_producer: separate`.

**Tests:** passing check then output edit; mismatched before/after; nonzero exit with `pass`; failed
manifest with passing rerun; refusal with zero captures; inline producer assessing itself.

## R4-08 — P1: Revalidation protects only preparation, not acceptance against a changed plan

**Locations:** design lines 647–659, 679–693, 763–768, 833–841; T25–T27.

Definition re-derivation happens at dispatch time; T25 applies only before the permit. Once running,
classification compares the result to `prepared`, not to the current canonical task. A plan edit
changing success criteria, verification, dependencies, or outputs can therefore leave an old result
valid against its old contract and eligible to mark the new task definition DONE. Revision checking
only causes a reload; it does not establish semantic compatibility with the new plan.

Even the hash's field list is inconsistent. §11.2 says `checks_interpretation` is inside it, but §6.6's
definition set omits that object. Contract `working_directory`, resolved root identities, host
capabilities and execution limits are also absent. Changing cwd can change what identical relative
paths mean without changing the declared task. `prepared` itself has no specified integrity binding
that compensates for these omissions.

**Required change:** define the exact normalized hash object in one place, with examples. Distinguish
semantic identity from attempt identity and initial snapshots. Recheck current task identity,
authorization, dependency satisfaction, and canonical attempt binding during the accepting commit;
repeat after revision conflicts. Keep baseline immutable, but check the relevant read-only identities
at the boundary being claimed. Add invalidation/withdrawal paths through publication and verification,
including acceptance-written-but-not-committed.

**Test:** prepare, launch, edit plan at a new revision, publish the old valid result, and attempt commit.
Repeat with changed cwd and changed interpretation only. Unrelated task updates must remain legal.

## R4-09 — P1: Correct claim comparison cannot repair incomplete claim derivation

**Locations:** design lines 679–688, 926–950, 1057–1079, 1418–1425.

The relation is improved; its inputs are still insufficient:

1. Read claims come from dependency outputs and check subjects only. An analysis task reading an
   existing source file, or an edit reading configuration/imported files not produced by a dependency,
   has no way to declare those execution inputs. `inputs_enumerated` can be true while these reads are
   wholly absent. A concurrent writer can change what the task is reasoning about.
2. Repository operations that branch/rebase/change checkout are assigned only `.git` write claims.
   Operations that change the working tree also need claims on that tree. `.git` and `src/a.py` are
   siblings, so those grants do not conflict. Git explicitly documents that switching branches updates
   the index and working tree. [Git switch documentation](https://git-scm.com/docs/git-switch).
3. Every check contributes only read claims. Tests, linters, builds, and explicit shell checks may
   write caches, generated files, or other subjects. There is no per-check write/effect set.
4. A global check is allowed a directory subject, while hashing is regular-files-only. The required
   subject digest maps cannot represent the example's repository-root snapshot without a new rule.
5. Only `project.json` is explicitly coordinator-reserved. There is no derivation rejection for worker
   claims overlapping `evidence.md`, `spec.md`, `INDEX.md`, the execution store, or the registry itself.
   A cooperative worker given such an output receives mutually inconsistent instructions.

Admission can exclude two *declared* overlapping operations; it cannot prevent undeclared writes.
Lines 1078–1079 and 1421 incorrectly call this prevention despite U2 acknowledging the absence of it.

**Required change:** add explicit task read sets and check read/write/effect sets, or restrict v1 to
task shapes where they can be exhaustively enumerated. Represent unknown scope conservatively with a
coarse claim or refuse concurrency. Derive Git claims per operation, not from one `.git` shortcut.
Reserve protocol-owned paths including ancestor overlap, with explicit coordinator-only exceptions.
Either define directory snapshot semantics or refuse directory-subject verification initially.

**Tests:** analysis reads a concurrently edited file; branch switch versus scoped writer; two checks
writing one cache; global-check snapshot; worker output targeting an ancestor of canonical state.

## R4-10 — P1: The proposed canonical mutations do not satisfy the real validator

**Locations:** design lines 883–889, 1449–1465; `workspace_lib.py:514–553, 1266–1267, 1335–1349`.

The documented withdrawal changes task status and `authorization.status`, but keeps the previous
explicit authorization's `source` and `authorized_at`. The real validator requires both to be null
unless status is `explicit`. A direct probe of `_validate_authorization` with a BLOCKED/denied task
and those retained values produced:

```text
authorization: source and authorized_at must be null unless status is explicit
```

The same commit must also set a nonempty `block_reason`, remove the task from `current_tasks` and
`execution.attempts`, and preserve the old authorization in durable history. Those candidate details
are not specified. Retrying BLOCKED says to preserve its reason in the task's `notes`, but `notes` is
not an allowed task field, and `block_reason` must be cleared when moving to TODO.

Withdrawal orders canonical commit before disposition, whereas §6.3's only reconciliation recovery
sequence orders disposition before commit. A crash after the withdrawal commit leaves a quarantined
attempt whose task is already BLOCKED, a combination not handled by the RUNNING-only table.

**Required change:** specify complete candidate transformations, not just status arrows. Use a
single reusable mutation builder for status, reason fields, authorization, `current_tasks`, attempt
binding, and evidence. Preserve consent history in immutable evidence, not disallowed task fields.
Make withdrawal follow one durable intent/commit/ack sequence and cover every prefix.

**Tests:** validate entire candidates using the real validator, not membership in enum constants.
Include prelaunch withdrawal, BLOCKED-to-TODO retry, and interruption after the withdrawal commit.

## R4-11 — P1: Planning-time checks and several supported task shapes lack a storage contract

**Locations:** design §5.5, §6.5, §8.1, §11.2; `workspace_lib.py:406–438, 1288–1290`.

Checks are said to be resolved once at planning time, but schema v4 is exactly v3 plus an execution
block containing only ownership and active attempt ids. The task schema still has only free-text
`verification`. Where is the approved structured interpretation persisted before an attempt exists?
Re-deriving it at every preparation is not “resolved once”; putting it in extra task fields violates
the promised v4 shape.

The contract drops `outputs[].required`, which the canonical output object requires. Thus required
and optional outputs cannot be distinguished in contract validation. The decision record promises
admission rejects required outputs with no covering check, but the normative admission list has no
such condition. Empty verification is offered as the empty-check case even though v3 requires a
nonempty verification string and v4 claims not to change that rule.

External references are still treated as filesystem paths by the blanket relative-path, writability,
resolution, and digest rules. Existing canonical `external` outputs are URL-like references, not an
absolute or relative local directory. The new external claim namespace does not define their mapping.
External-effect tasks also need the existing canonical effect receipts, absent from the result schema.

**Required change:** choose an authoritative, versioned location for approved check definitions and
explicit resource declarations. Preserve all output semantics in the contract. Use tagged local and
external references, with root-specific validation and effect receipt handling. Alternatively,
explicitly refuse unsupported task shapes and narrow the claim that inline handles all existing work.

**Tests:** approved interpretation survives restart/retry unchanged; optional missing output; required
uncovered output; external receipt task; nonexistent-yet local output; valid nonempty inspection text.

## R4-12 — P2: Publish-if-absent still leaves durability and conflicting-publication gaps

**Locations:** design lines 704–735, 757–768; §5.5 and §5.7.

The hard-link approach is a useful choice, but its contract needs completion:

- If a prior attempt linked `final` and crashed before directory sync, an identical-byte retry returns
  success without a stated sync obligation. That retry has not established the claimed durability.
- Newly created attempt/capture directory entries also need a durability policy; syncing only the
  innermost directory does not establish persistence of every newly created ancestor entry.
- Worker outputs are written before the manifest, but there is no requirement to flush/sync them.
  “Written first” establishes visibility ordering, not persistence ordering after machine failure.
- A differing second publication is refused, so the conflicting bytes are not in the final record.
  The classifier cannot infer that a conflict happened from the existing publication alone. The worker
  detecting it is forbidden to write the coordinator-owned hold. The durable reporting path is absent.
- Unknown/corrupt coordinator records need holds too, but T9 permits them only from PUBLISHED and
  finalizable attempts categorically prohibit new holds. Presence-only labeling could trust corrupt
  `release`/`commit-observed` records unless validation precedes every projection.

File sync and directory-entry sync are distinct requirements; Linux's manual explicitly calls out
the directory step. [fsync documentation](https://man7.org/linux/man-pages/man2/fsync.2.html).

**Required change:** state the failure model (process crash versus OS/power loss), supported filesystem
capabilities, and all required persistence barriers, including the identical-retry path. Specify
temporary-file recovery, I/O error handling, and durable collision evidence outside the conflicting
record path. Validate records before projecting them; corruption must not be hidden by a “terminal”
display label. Test the real helper with injected failures, not only record-set arithmetic.

## R4-13 — P1: The version gate does not exclude legacy writers in other projects

**Locations:** design lines 327–370, 1036–1047, 1573–1586.

Migrating project A to v4 stops an old installation operating on A. It does not stop that installation
operating on v3 project B in the same workspace, targeting the same repository. B never reads the
registry, so its sequential writer can conflict with A's correctly granted worker. This is within the
stated same-workspace scope, not the explicitly deferred cross-workspace case.

Reader rollout is also underspecified: a Stage-4 installation understands v4 before dispatch/recovery
ships. It must not run old sequential execution on a v4 project merely because its schema is readable.
“Readers understand v4” needs explicit read-only versus mutating-command behavior.

Closure has no execution-store gate either. A project can have a PREPARED/TODO attempt holding claims
without a RUNNING task. Existing task-level cancellation checks are not enough to establish that
collection may safely remove the journal or that the project has relinquished all authorities.

**Required change:** define the operational activation boundary. Either require all writers to the
target/workspace to participate and enforce that through a common entry point, or explicitly make
quiescing legacy writers a prerequisite and limit the guarantee accordingly. Disable mutation by
readers lacking executor/recovery support. Gate closure/collection on resolved launch/check
authorities and released grants, not just project/task status.

**Tests:** v4 A and legacy v3 B share a target; reader-only host opens active v4; cancellation with a
prepared grant; collection with unresolved inline execution. Do not claim mixed-version safety from
testing refusal of a single v4 file alone.

## R4-14 — P1: Canonical receipt lookup and finalization need identity checks, not status equality

**Locations:** design lines 589–592, 825–847, 1339–1347; §5.7.

The receipt reuse improvement is real, but “canonical evidence” is not defined precisely enough.
`evidence.md` is written separately and before the `project.json` commit point. Finding the receipt
in that Markdown file cannot prove the task commit landed. The rule must identify the exact canonical
task evidence reference in `project.json`, bound to this attempt and receipt.

Similarly, replaying a disposition by checking only whether its target status is already present is
unsafe after task retry or an intervening canonical change. The status `TODO` is not proof that this
attempt's reset was applied. Every mutation must compare the active attempt binding, or recognize an
immutable canonical reference proving this particular mutation landed. The design introduces that
binding but does not give its update/replay rules.

Retention preserves a referenced acceptance and its captures/logs, but not necessarily the prepared
contract, classification, or result that explains them. A retained `definition_hash` without the
definition it hashes is not independently auditable. Raw capture ids in the receipt also need an
unambiguous path/digest lookup after collection; the layout is named by check/sequence, not capture id.

**Required change:** define one stable canonical reference tuple and exact evidence append dedup key;
separate precommit evidence publication from proof of canonical acceptance. Include expected attempt
identity in finalization operations. Retain the transitive definition/result evidence needed to
interpret receipts, or make receipts self-contained enough to validate without it.

**Tests:** evidence append succeeds but canonical commit fails; status matches due to another attempt;
accepted receipt survives later revisions; collected execution remains auditable without hidden files.

## R4-15 — P2: The model's strongest advertised guarantees are not actually tested

**Locations:** model lines 168–223, 342–391, 643–675, 900–940, 996–1045; design §20.2.

The model is valuable for label ordering, but its limitations exceed the stated “not real hosts or
filesystems” disclaimer:

- `apply_outcome` discards its input prefix and returns `len(TRANSITIONS[transition])`. Idempotence is
  therefore true by construction even for the ambiguous dispatch prefix. It does not apply recovery.
- “No redispatch” checks that explanation strings lack two phrases. It does not count physical starts.
- The graph omits canonical state, live execution, generations, actual grants, and most guards. The
  release-safety test checks whether a released attempt still *displays* QUARANTINED/UNCERTAIN/STOPPED.
  Writing disposition changes that display, so the test misses release without termination.
- `fields_ok` forbids fields of the other capture kind but never requires its own fields. The default
  capture has an empty field set and is adequate. `not_run` is tested by setting field names, not an
  exit-status value. Artifact identities, argv equality, and stream digests are not represented.
- The manual graph is not compared with the document's guards/record writes. A canonical enum checker
  does not provide that comparison. Existence of a hypothetical escape path is also weaker than
  recovery liveness on a host without the evidence needed to take that path.

**Required change:** replace the string/prefix model with a small stateful reference model containing
canonical revision and task binding, two coordinator identities, grants, launch capabilities, live
executions, validated journal records, and artifact versions. Recovery must produce operations and
apply their effects. Inject crashes between those effects and replay until quiescent or explicitly
awaiting evidence. Assert external invariants, not names of derived labels.

Keep the model stdlib-only and design-stage; no production executor is needed. Use table-driven
tests for complete capture objects and actual canonical candidates. A small model with ten adversarial
interleavings is more informative than hundreds of self-consistent label combinations.

## R4-16 — P2: Host claims need versioned evidence, and fallback errors need classification

**Locations:** design lines 1429, 1515–1549; contract `host` and capacity §8.4.

“Claude Code ... discover and interrupt are not currently available” is an unqualified host-wide
claim. Current official documentation describes `TaskStop`, task observation, and resumption, with
version-specific behavior. That does **not** prove a deployed adapter can meet this protocol's exact
discovery or termination guarantees; it does mean blanket absence is not a defensible capability
assessment. The same documentation says some stopped/completed agents can resume under the same id,
which directly matters to R4-03. [Claude Code subagent documentation](https://code.claude.com/docs/en/sub-agents#resume-subagents).

The generic failure row “spawn or discover error → inline” also conflicts with the conservative
`ambiguous` contract. An RPC error after a host accepted a start must retain uncertainty; it must not
trigger inline replay. Missing `atomic_link` refuses only *parallel* execution in §17, even though
inline uses the same journal primitive. Lowering capacity cannot repair missing persistence.

**Required change:** publish a host/version/operation matrix backed by tested adapter behavior.
Separate host UI features, model-exposed tools, and adapter guarantees. Distinguish preflight absence,
definite no-start, ambiguous start, and observation failure. Gate the entire protocol on storage
correctness prerequisites. State Codex/Kimi behavior explicitly under the repository's cross-host
contract, even if the first supported mode is inline only. Define whether reported capacity includes
the coordinator and other active agents; reserve launch slots before asynchronous dispatch.

No installed host behavior was tested in this review. The cited documentation is evidence for
narrowing the claim and planning conformance tests, not evidence that an adapter already works.

## R4-17 — P2: The decision record again overstates what changed and what the checks establish

**Locations:** design lines 466–470, 544–554, 1627–1629, 1682–1708; decisions review-03 dispositions.

The reconstructed revision-1 history is substantially better. New historical mistakes remain:

- The design/model attribute fourteen-row presence-based labels, `release` conjuncts, and T28/T29 to
  revision 3. `git show fd24db1:.../parallel-execution.md` instead shows a mutable `state.json`
  lifecycle and only T1–T25. These may be defects found in an intermediate revision-4 draft, but
  they are not defects present in committed revision 3. Label the draft accurately.
- Decisions R3-05 and the model's regression test say the previous status was `revoked`. The actual
  T21 used `authorized`; the hold reason was `authorization_revoked`. Test the actual regression.
- S02 says revision 4 has no §14 subsections; it has §14.1. S17 claims stop forbids further checks,
  which §14.1 does not say. S20 claims output coverage is checked at admission, which §8.1 omits.
  S29 claims a Stage-8 reassessment exists in §19; its row only says reporting and benchmark.
- S28 says revision 3 had twenty-one citation rows; its checked-in table had twenty-six.

The checker still permits a canonical cell containing garbage, because it extracts only recognized
arrow pairs and ignores the rest if any valid pair exists elsewhere. It ignores link fragments and
does not check opposite release orders. Its fixed `REVIEW_FILES` map is not “every review file
present,” and decision-section numeric references are not all validated. These are reasons to narrow
its promise, not to build a prose theorem prover.

**Required change:** replace “green means internally consistent” with the exact checked relationships.
Parse every canonical cell as either `none` or a complete supported grammar; reject leftovers. Check
local fragments if claiming link resolution. Track review coverage explicitly through an assessed-
through revision, so adding a new unanswered review does not itself demand a disposition. Remove
historical explanations from the normative rules where they obscure the current contract.

## R4-18 — P2: The efficient implementation path needs a smaller state core and bounded work

**Locations:** design §2, §4, §5.2–§5.3, §8, §15, §19–§20.

The document has grown from 1,246 to 1,759 lines, with a 1,169-line model and a 666-line checker,
while “the machinery mostly exists” and “the protocol is thin” remain. That framing hides the cost:
this is now a new execution journal, workspace admission service, migration, capture engine, recovery
system, and several host adapters. A durable filesystem is a storage choice, not proof of simplicity.

Specific implementation costs are missing:

- Every admission scans all grants; every decision is described as a query over all retained records.
  One heartbeat file every 30 seconds means 11,520 files per day at four continuously active workers.
  Retaining them for a long project while rescanning the full history can dominate tiny tasks.
- The design says `runtime.json` is never read for decisions but also uses cached volume probes from
  it in derivation. Correctly invalidated in-memory projections are useful; they need not become a
  competing durable authority. The ban should be on trusting unvalidated cache state after restart.
- Admission has no concrete ready-loop order, fairness/tie-break rule, maximum in-flight preparations,
  or rule for processing a result arriving between a gate scan and launch. Capacity counts “dispatched”
  workers but not explicitly reserved/ambiguous launches.
- The benchmark is one project run three ways, without controlled starting state, repeated samples,
  cache/model settings, or a small-task overhead budget. It cannot establish an economical default.

**Required change:** start with a minimal stateful core and an explicit ready-loop. Use an in-memory
dependency counter/ready heap, cache immutable parsed records by validated identity, and scan only
active attempts on routine ticks. Rebuild from durable authorities on ownership change. At four
workers, a straightforward locked scan of active grants is reasonable; do not optimize historical
heartbeat files into the admission path. Consider making advisory heartbeats disposable rather than
permanently journaled, with durability reserved for decisions.

Benchmark the two admitted task shapes plus a linear and a tiny-task case, repeating runs from
equivalent inputs and recording total tokens, failures/recovery cost, and quality. Set a measured
minimum delegation granularity. Keep optional transports and alternate stores out of the first
implementation unless those measurements justify them.

## Smaller details requiring correction

These are separate consistency/implementation details, not additional principal severity counts.

| ID | Location | Issue and correction |
|---|---|---|
| S4-01 | §5.2 heading/text | It says “two predicates,” then three questions. “No field is consulted” is false: disposition resolution, schema, identity and validity fields matter. Say projections use validated records, not just file presence. |
| S4-02 | Architecture diagram, line 129 | `execution: {generation, attempts}` does not match the actual `ownership_generation` and `coordinator_run` fields. Use the schema's names. |
| S4-03 | §5.4, line 306 versus §7.5 | Receipt hashing omits `receipt` in one place and `receipt_id` in the other. The former is the wrong field and creates a self-reference problem. |
| S4-04 | §5.4 | “Trailing byte if any” permits two encodings. Choose exactly one trailing-newline rule. Specify array ordering and reject lone surrogates/invalid Unicode rather than relying on runtime encoding errors. |
| S4-05 | §5.4–§5.5 | Capture is called content-addressed, but `capture_id` is an opaque non-digest id and no separate capture digest field/reference is defined. Specify where the content digest lives and what excludes self-referential fields. |
| S4-06 | §5.4 | Sequence is described as a four-digit padded integer, while the model uses unbounded integers. Separate JSON value from filename encoding and define the overflow policy after 9999. Heartbeat sequences have no check/owner namespace. |
| S4-07 | §5.1–§5.5 | Several record types have no full field schema: launch timestamps, stop-evidence identity, hold details, disposition evidence, and release preconditions. “Every field is specified” is not true of a one-line “carries” table. |
| S4-08 | §5.5 | Unknown-major handling must cover unknown record kinds, wrong JSON top-level types, boolean-as-integer values, and malformed nested fields. For content hashes, define whether unknown minor-version fields are included before ignoring them. |
| S4-09 | §5.5 enable-execution | “Refused twice ... exits zero” is confusing. Define an already-enabled no-op, including whether stale expected revision is checked before that return. Specify `updated`, protocol version validation, and the required null coordinator value. |
| S4-10 | §6.5 | Positive bounds are missing for deadline, heartbeat interval, and log cap. A deadline budget has no durable start instant. Specify UTC deadlines plus monotonic elapsed accounting within a process and restart treatment. |
| S4-11 | §6.6 | `inputs_enumerated: false` is global, yet the guarantee is discussed per dependency. List which inputs are not covered and why. A dependency output also writable by this task must be removed from the read-only baseline. |
| S4-12 | §6.6, §8.2 | Case probing has no specified safe directory or failure behavior. Probe per relevant volume without undeclared target writes, then define how comparison uses the answer. Admission-time symlink rejection alone does not prevent later path retargeting by outside actors. |
| S4-13 | §6.6, §13.3 | Produced/partial comparisons omit deleted files, absent-still-absent files, changed type, unreadable files, and unsupported directory outputs. Use a tagged outcome, not three assumed digest cases. |
| S4-14 | §7.1 | “Prepared plus its logs” does not match the layout; prepared has no logs. The primitive must constrain temp and final to the same filesystem, handle short writes, and normalize I/O failures. |
| S4-15 | §7.3 | Missing capture files, invalid capture schemas, and unrecognized outcome values lack explicit classifier placement. Conflicting publication is a separate observed event, not generally a pure property of one final manifest. |
| S4-16 | §8.1 | “All conditions” includes delegability, then allows nondelegable inline work. Express `eligible(task, executor)` or separate executor selection from universal admission. Check project is EXECUTING, not merely task TODO. |
| S4-17 | §8.2, line 922 | The relation is symmetric, not “one-directional.” The examples immediately demonstrate symmetry. |
| S4-18 | §8.2 | External keys are called opaque but interpreted hierarchically. Define escaping/canonicalization of slash segments. Reject empty keys and decide whether case/URI normalization applies. |
| S4-19 | §8.2, §11.1 | A coordinator check is selected when it needs claims the worker does not hold, yet all checks must be included in the same up-front attempt grant. Distinguish executor permissions from attempt-owned reservations. |
| S4-20 | §10 | Captures and result/heartbeat files must be writable but are not included in the output-derived grant list. Specify the implicit attempt-owned store namespace and its limits instead of instructing workers to violate their literal claim set. |
| S4-21 | §11.2 | Splitting `&&`, `\|\|`, and pipelines into several unconditional checks does not preserve command semantics. Only offer semantics-preserving splitting; otherwise require explicit shell execution. Quoted literal `;` or `\|` is not automatically composition. |
| S4-22 | §11.3 | Command captures forbid `assessor_*`, but assessment fields are `criteria`, `rationale`, and `assessor`. Match the actual names. Define exact exit-status variants and whether code/signal is forbidden for timeout/not-run. |
| S4-23 | §11.4–§11.6 | Higher owner precedence must not let an arbitrary coordinator assessment replace a required human assessment. Define compatibility of kind, identity, actor relation and executor before precedence. Repeated execution is not side-effect idempotence. |
| S4-24 | §13.2 | The first-heartbeat fallback needs a required `launch` timestamp; §5.2 only promises a handle. There is no coordinator heartbeat to apply the same stale-owner bound to in §12.1. |
| S4-25 | §15 | “Project lock timeout 30s, as today” is wrong: `DirectoryLock` and `commit_candidate` default to 5.0 seconds. New configurable settings also need an authoritative storage location. |
| S4-26 | §18 | Most U1–U12 rules are not marked `[UNENFORCED]` where they appear despite the introduction promising that. Distinguish cooperative trust boundaries from rules the proposed implementation should actually validate. |
| S4-27 | §19 | Stage 6 migration plainly changes observable behavior and permanently gates old readers, contrary to “Stage 7 is the only one.” The non-downgrade rule also contradicts every stage being independently revertible. |
| S4-28 | §5.7, §20.3 | Define maximum result/rationale sizes and how bounded stream draining and redaction work without unbounded memory or pipe blockage. Include journal I/O volume and evidence-read cost in the benchmark, not only commits. |

## Suggested order of implementation work

Do not begin by adding more label rows or more prose-matching assertions. First settle the operations
and their invariant boundaries. A compact implementation plan is:

1. **Write a small invariant ledger.** At minimum: one executable write authority per conflicting
   resource; no release while a launch/check can still write; acceptance bound to current task and
   checked bytes; canonical receipt proves commit; malformed authority records never become absence.
   Name the authority and synchronization point for each invariant.
2. **Choose the initial safety envelope.** Shared filesystem, one workspace, upgraded participating
   writers, no untracked detached processes, explicit input/output/check resources. Decide whether
   takeover requires predecessor termination. Refuse unsupported cases visibly.
3. **Define complete record and operation shapes.** Include preconditions, durable effect sequence,
   identity/generation checks, and replay behavior. The display label is an output of this model,
   not the permission to record asynchronous facts. Reduce overlapping status representations.
4. **Build the stateful model before the executor.** Two coordinators, two tasks, one shared path,
   one checker, and one launch capability are enough to expose the main races. Model canonical state
   and external execution separately. Make the findings' traces regression cases.
5. **Implement storage and canonical mutation adapters.** Publish-if-absent and registry tests should
   use temporary directories and injected failures at every persistence boundary. Canonical candidate
   tests should use the real validator. Keep project lock and post-commit index handling compatible
   with the existing transaction path.
6. **Implement capture/acceptance as a pure validation pipeline around bounded process I/O.** Validate
   complete tagged records, derive verdicts, bind snapshots, select captures, and publish one immutable
   acceptance intent. Prove evidence deduplication on interrupted commits.
7. **Add one host adapter and conservative recovery.** Separate start, observation, sealed outputs,
   and irreversible release. Add host-specific conformance cases before enabling parallel dispatch;
   keep unsupported hosts on an explicitly supported, equally safe path.
8. **Only then optimize scheduling.** Use active-state projections and a ready queue, measure overhead,
   and introduce transport/storage changes only for observed bottlenecks. Keep the normative document
   focused on this contract; put historical explanation in the companion.

## Verification performed

These commands completed against the reviewed checkout:

```sh
.venv/bin/python -B -m pytest -q --no-cov -p no:cacheprovider -o log_cli=false \
  tests/plugins/research/test_parallel_execution_doc.py \
  tests/plugins/research/test_parallel_execution_model.py
# 75 passed, 1228 subtests passed in 1.61s

.venv/bin/ruff check --no-cache .
# All checks passed!

.venv/bin/ty check .
# All checks passed!
```

Additional read-only, in-memory probes produced:

| Probe | Observed result |
|---|---|
| Classified result with no acceptance or commit observation | `dispatch_blocked = False` |
| Model T22 then T24 from launched quarantine, with no stop evidence | `release` present, `holds_claims = False` |
| Command capture with empty kind-specific field set | `adequate = True` |
| Empty check list | Model `canonical_intent = DONE` |
| Apply model recovery to ambiguous dispatch prefix | Returns completed prefix `3` unconditionally |
| Real authorization validator after status-only withdrawal | Rejects retained source and authorized_at |
| Replace T2 canonical cell with `garbage` | Documentation `run_checks` reports no findings |
| Reverse §8.5 release ordering | Documentation `run_checks` reports no findings |
| Add nonexistent fragment to companion link | Documentation `run_checks` reports no findings |

The release-order mutation is a demonstration of the checker boundary, not a demand that a Markdown
checker understand concurrency. The actual design already contains both orders.

Full regression/coverage, production concurrency, power-loss behavior, and installed host conformance
were not tested. External primary documentation was checked only for the specific claims linked
above. No executor, design, tests, plugin configuration, release metadata, or previous review was
modified by this review; the added artifact is this file.
