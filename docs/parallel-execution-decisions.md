# Parallel execution design — decisions and history

Companion to [`plugins/research/skills/project/references/parallel-execution.md`](../plugins/research/skills/project/references/parallel-execution.md),
which is the normative reference: current rules, operations and failure behaviour.

This document holds everything that is *about* that document rather than in it — the revision
history, the disposition of every expert review, and the alternatives that were rejected, deferred or
revisited. Reviews argued the reference was becoming a response to its own reviews; splitting them is
how that stops.

Nothing here is normative. Where this document and the reference disagree, the reference wins and
this one has a bug. The reference is also **self-contained**: it is shipped inside the plugin and
`docs/` is not, so no runtime rule depends on anything written here.

**Section numbers in the disposition tables refer to the revision named in that table's column
heading.** Revisions 4 and 5 both renumbered sections; §23 of the current reference is the index of
what it actually cites today, and each disposition section below records the revision it was
assessed against and the revision that answered it.

## Revision history

Reconstructed from the commits on this branch and the checked-in review files, not from recollection.
Each row names the commit that is the revision.

| Revision | Commit | Date | What it actually contained, and what caused it |
|---|---|---|---|
| 1 | `e18e589` | 2026-09-11 | First design. §2 already carried the graph measurement — mean average level width 1.86, maximum 3.87, modelled speedup 1.45x mean — and made no 2-4x claim. §3 already cited `DirectoryLock` and `commit --expected-revision` as existing facilities. The architecture was **one SQLite database per project at `<project-dir>/queue.db`** in WAL mode (§5), a hand-rolled JSON-RPC 2.0 MCP server over stdio for worker access (§6), and a **ready queue as the primary scheduler with wave-synchronous scheduling as a fallback** (§7.4). |
| 2 | `3eabe49` | 2026-09-11 | Rewritten after [review 01](parallel-execution-review.md). Replaced the database and the MCP server with a file store inside the project directory; removed the wave-synchronous fallback in favour of continuous dispatch; withdrew the lock-contention argument; added the recovery contract, the resource model, the unenforced-guarantees table and a citation table; recorded nine conditions for reconsidering a database and an MCP transport (restored below). Its introduction miscounted review 01's findings as seven high-priority. |
| 3 | `fd24db1` | 2026-09-11 | Rewritten after [review 02](parallel-execution-review-02.md). Added the attempt transition table, `DISPATCHING`, the publication protocol, the receipt/reference/acknowledgement separation, record schemas with a version gate, the acquisition policy, coordinator run identity and lock order, the versioned contract object, the method/actor/capture model, host-adapter contracts and a retention policy. Deferred staging. Split this document out of the reference and committed the citation checker as a repository test. |
| 4 | `812010c` | 2026-09-11 | Rewritten after [review 03](parallel-execution-review-03.md). The store became a **journal** whose labels and gates are pure functions of the set of records present, published with a publish-if-absent primitive; reservations moved to a workspace-scoped registry every executor including inline must use; identity split into three sets so an intended edit invalidates nothing; the authorization predicates were rewritten against the real canonical enums; takeover became a revision-checked canonical commit; checks are resolved at planning time and shell composition is rejected rather than downgraded; captures became tagged records with per-check adequacy; `copy` and `worktree` modes were deferred. This document's fabricated history was deleted. |
| 5 | `44f1388` | 2026-09-11 | Rewritten after [review 04](parallel-execution-review-04.md), which is where the narrowing happened. §3.1 lists the nine task shapes v1 supports and §4 gives thirteen refusal codes, so an unsupported shape refuses visibly by code instead of being promised vaguely; ten items moved to §22 as deferred. The label graph is replaced by a fact set (§6.1) and a [derived display label](../plugins/research/skills/project/references/parallel-execution.md#62-the-derived-label) that gates nothing. Seventeen operations (§8.1) share one `intent → commit → ack` shape, so every crash prefix has exactly one completion (§8.2). Takeover requires one of three proofs of death (§12.2) and staleness is demoted to a trigger; journal writes are fenced separately from canonical ones (§12.4). The dispatch gate closes on classification and opens on resolution (§14.2), and the failure budget is computed from ordered resolutions so it survives a restart (§14.3). `runtime.json` becomes a discardable mirror and the probe results a derivation reads move to `config.json`. Five invariants (§1) replace the prose guarantees, and the model test became a stateful reference model with crash injection (§21.2). |
| 6 | *this revision* | 2026-09-11 | Rewritten after [review 05](parallel-execution-review-05.md), which recommended stopping the global design rounds. An acceptance became a directory of `intent` + numbered `fences` + `ack` per mutation (§7.1), so a retry publishes a fence rather than rewriting an immutable record; `capture_id` became a derivation of the capture's own digest; the stop gate and `open_causes` became durable and project-level; scope ids became one per check *execution* with a stated filename encoding; O19 `acquire` and O20 `relinquish` joined §8.1 and §12.3 states fence and candidate separately for all four cases; §7.4 split into a common and an attempt envelope with four project-level kinds carrying the common one only; §7.5 replaced the fsync claim with durability classes. Two defects the reference model found and no review named are fixed: §7.2's `identical` compares `content_digest` rather than bytes, and §12.4 fences the store root as well as each attempt. §20 is six phases instead of eight stages, with Phase 1 specified to implementable detail in [`parallel-execution-implementation-plan.md`](parallel-execution-implementation-plan.md) and the rest adjustable after it. |

## What was wrong with revision 1, established from the commit

Review 03's finding R3-16 said this document described a revision 1 that does not exist. That was
checked rather than conceded: `git show e18e589:plugins/research/skills/project/references/parallel-execution.md`
contradicts all four of the claims the earlier text made, so the corrections below are the record.

- **Revision 1 measured the workload.** Its §2 already contained the 1.86 / 3.87 / 1.45x table.
  There was no 2-4x speedup claim to withdraw, and no "measurement error that produced revision 1".
  The section that said otherwise is deleted rather than reworded.
- **Revision 1 never asserted that `research:project` has no cross-process locking.** Its §3 cited
  `DirectoryLock` and `commit --expected-revision` as facilities that already exist. The "§3.1
  void-lock correction" described a claim the commit does not contain, and is deleted.
- **The queue was per project, not at the workspace root.** `<project-dir>/queue.db`.
- **Wave-synchronous scheduling was the fallback, not the design.** The ready queue was primary.

What revision 1 *did* get wrong is a different thing, and it is worth recording accurately because it
is the argument that selected a database and a server:

> **The contention argument.** Revision 1 argued (lines 105–109, 179–180, 473) that a file store
> would contend on `.project.lock`: workers appending to `evidence.md` under that lock, the
> `record-evidence` subcommand having no `--lock-timeout` flag and therefore being pinned at the 5.0 s
> default, and a commit regenerating both root caches under a second lock.

One of those three premises is true and the conclusion does not follow from it. `record-evidence`
genuinely has no `--lock-timeout` flag — the flag exists on `initialize`, `commit`, `rebuild-index`,
`promote-memory` and `migrate` and nowhere else — so its timeout is the 5.0 s default. But the lock is
not held across the work: `record_evidence` runs the subprocess first and calls
`_append_evidence_entry` afterwards, so the lock covers a read-modify-write of one Markdown file and
not the command's runtime. And `commit_candidate` rebuilds the index *after* its `with DirectoryLock`
block has exited, so the index rebuild is not inside the commit's critical section. The contention
revision 1 projected was the contention of a design that holds a lock across execution, which is not
the design of the existing code.

**The lesson worth keeping** is the one already in workspace memory as
`measure-the-shape-before-the-engine`, but the version of it revision 1 needed is narrower: the
premise that selects a mechanism must be measured, not projected from a plausible reading of the code.
Revision 2 replaced the store; revision 4 states in §3 what the existing facilities do and cites each
claim to a file, a line range and a literal needle that the repository test re-reads (§22), so a claim
about existing behaviour fails a test rather than surviving a review.

## Review 01 — disposition

[`parallel-execution-review.md`](parallel-execution-review.md), assessing `e18e589`. Assessed against revision 1; answered in revision 2. **Nine findings: six high-priority
and three medium.** Revision 2's introduction counted seven high, and revision 3's decision record
counted six high plus *one* medium. Both were wrong; the table below carries all nine with the
subjects and priorities the review file actually gives them.

| # | Priority | Finding | Disposition in revision 2 |
|---|---|---|---|
| 1 | High | The queue is not reconstructible as claimed, and draining can lose results | **Accepted.** The database was removed; the file store's records became the authority, and §13 gained an interruption table. Revision 4 replaces this with the journal, where reconstruction is a pure function of the record set (§5.2). |
| 2 | High | Lease expiry can create two writers for the same output | **Accepted.** Leases were removed. A missed deadline became evidence of nothing, and automatic retry after lost contact was deferred (still deferred in revision 4, §13.2). |
| 3 | High | The start protocol and stale-plan behaviour are unspecified | **Accepted.** A start protocol with an explicit contract, and re-derivation against the current plan. Revision 4's §6.6 and §8.1 are the current form. |
| 4 | High | Disjoint declared outputs are insufficient isolation | **Accepted.** The resource model: claims, namespaces and a comparison rule, rather than a set-disjointness test on declared paths. |
| 5 | High | SQL predicates do not establish worker identity | **Accepted.** Dissolved with the database. Identity became the attempt id plus the contract hash, checked when a result is read. |
| 6 | High | Class B evidence weakens a guarantee the repository deliberately added | **Accepted.** The evidence classes were removed; verification evidence is a capture with a recorded actor, and the coordinator judges it. |
| 7 | Medium | The lock argument does not justify the MCP architecture | **Accepted.** The MCP transport was deferred. Revision 4 states what the lock argument actually was and why it does not hold — see above. |
| 8 | Medium | Transport and runtime compatibility need concrete gates | **Accepted.** Concrete host and runtime conditions, restored below as the database/MCP adoption conditions. |
| 9 | Medium | Performance conclusions exceed the measurement | **Accepted.** §2 leads with the measurement, states that the model charges nothing for spawning, prompt authoring, integration or contention, and states what it does not measure. |

Revision 2 also added the unenforced-guarantees table on review 01's prompting: several of the
design's rules are prompt text, and a design that does not say so implies enforcement it does not
have. It survives as §18.

## Review 02 — disposition

[`parallel-execution-review-02.md`](parallel-execution-review-02.md), assessing `3eabe49`. Assessed against revision 2; answered in revision 3. Ten principal findings — seven
P1, three P2 — nine additional implementation issues, and fourteen smaller corrections.

**Accepted in full on substance.** Every checkable claim in the review was re-verified against source
before disposition, and all ten findings hold. Four points are answered rather than adopted as
written; those are below the table, and each is a disagreement about the fix, never about the defect.

| # | Finding | Disposition in revision 3 |
|---|---|---|
| 1 | A crash during spawning is indistinguishable from never-launched | **Accepted.** Durable `DISPATCHING` before the host call (T3, T4); no handle plus no launch key resolves to `UNCERTAIN` (T6), never to redispatch; §17 requires `spawn` to be idempotent under a launch key, offer discovery, or report a definitive failure. |
| 2 | The canonical acceptance record is unspecified | **Accepted.** §7.2 separates receipt, canonical evidence reference and acknowledgement; a receipt carries **no** revision, so its identity cannot change on a lost race; recovery reads acceptance from the canonical reference. |
| 3 | Atomic replacement is not a multi-file immutable publication protocol | **Accepted.** §7.1's ordered publication: logs and captures first, hash, manifest last; identical republication is success; differing republication is rejected; incomplete publication is distinguishable from a failed result. |
| 4 | Reservations held through integration can deadlock on verification upgrade | **Accepted**, taking the review's first option. §8.3 acquires the complete execution-and-verification claim set upfront with a no-upgrade invariant; the two-task scenario is reproduced in the reference and is a required test. |
| 5 | Coordinator takeover is undesigned | **Accepted.** Run identity, ownership generation and the five operations; every mutation re-checks ownership; a superseded coordinator stops. Revision 4 closes the residual race this left (R3-02). |
| 6 | The contract hash omits execution-relevant state and has no derivation contract | **Accepted.** A versioned contract object with named fields including task name, `depends_on`, effect, authorization scope, working directory, resolved roots and mode; serialization, ordering, algorithm and absent-value treatment specified. |
| 7 | Staging does not provide the confinement automatic retry needs | **Accepted.** Staging **deferred**, automatic retry unavailable, and its return requires a verified confinement capability plus a promotion protocol. |
| 8 | State-machine paths that cannot release reservations | **Accepted.** A transition table with terminal states, where every terminal state releases reservations except quarantine, which holds them and pauses dispatch so nothing starves. |
| 9 | Bounded shutdown promised with no possible terminal action | **Accepted.** A five-step terminal action ending in "exit", with the reference stating plainly that it does not promise reconciliation completes. Recovery scans attempt states rather than trusting a `dispatch_paused` flag — step 4 of §14 in revision 3, and in revision 4 the flag does not exist at all (§7.3). |
| 10 | Verification provenance confused with validity | **Accepted.** The ranking is **removed**, not relabelled. Three independent axes with an admissibility table, plus the adequacy comparison that catches a capture for `true` and a post-check artifact modification. |

The nine additional implementation issues were all addressed in revision 3: separate namespaces with
a conflict matrix in which two readers never conflict; the workspace-root target claim with ancestry;
the split between a missing worker capability and a missing correctness prerequisite; heartbeats
reduced to advisory liveness with host observation as the authority; the fail-closed old-reader gate
as a `project.json` `schema_version` bump; an explicit list of what the capture helper must do
differently from `record_evidence`; one capture directory per verification command with a manifest;
and a retention policy. Review 03 reopened four of these on their details (R3-03, R3-08, R3-12,
R3-14), and the current form is revision 4's.

### Four points answered rather than adopted

1. **Finding 3 asked for a publication protocol; it did not need a new atomic-write helper.** The
   review's praise of `atomic_write_text` is accurate — it flushes, `fsync`s the descriptor,
   `os.replace`s, and `fsync`s the directory. The gap is ordering across files and immutability,
   which are protocol properties, not helper properties. Revision 4 revisits half of this: ordering
   is still protocol, but `os.replace` is genuinely the wrong primitive for a record that must never
   be overwritten, so §7.1 is now publish-if-absent over `O_CREAT|O_EXCL` and `os.link` rather than a
   sequence over `atomic_write_text` (R3-14).

2. **"Versioned JSON schemas" is met by field-level tables and a compatibility rule, not by JSON
   Schema documents.** Literal `.json` schema files would be implementation shipped inside a design
   document, and would need their own validator to mean anything. §5.2 specifies every field's name,
   type and meaning; §5.5 specifies major/minor semantics, additive minors, and unknown majors as
   *unreadable rather than absent*. Stage 4 owns whatever machine-readable form the implementation
   wants.

3. **Finding 10's ranking is replaced rather than re-labelled.** The review's `method` / `actor` /
   `capture` decomposition is adopted exactly, and the "Strongest / Strong / Weakest" ranking is
   deleted rather than rewritten with better labels, because ranking provenance at all is the error —
   a coordinator can run an inadequate command and get a perfect process record.

4. **Capacity one stays the sequential path; the refusal is separate.** The review is right that
   capacity one does not restore missing result delivery or persistence. But capacity one is not a
   degraded mode of this protocol — it *is* the sequential path, which is what the skill does today
   and which the reference defines as the same scheduler at capacity one (§4). So §17 splits the two
   cases: a missing worker capability gives an inline executor at capacity one with recovery intact,
   and a missing correctness prerequisite — durable delivery, durable persistence, `atomic_link` —
   means **the protocol does not start**. Revision 4 keeps the split and closes the hole review 03
   found in it: capacity one is not a fallback for a *failed reservation* (R3-01).

### Corrections to the design's own record

Review 02's smaller corrections were all applied in revision 3. Three were about the design's own
history rather than its rules, and two of the three have since been corrected again:

- **The finding count.** Revision 2's banner and commit message said seven high-priority findings.
  Revision 3 said six high plus one medium. Both are wrong: review 01 has six high and **three**
  medium (findings 7, 8, 9). The table above is the corrected record; review 03 did not name this
  error, and it was found while reconstructing the history R3-16 asked for.
- **The checker was not in the committed tree.** Revision 2 described a citation checker that lived
  only in the project workspace, which made a documented check unreproducible by anyone reading the
  branch. It is `tests/plugins/research/test_parallel_execution_doc.py`, inside the repository suite.
  Committing it outside `plugins/research/skills/project/scripts/` keeps it clear of the
  `fail_under = 100` coverage gate that applies to that package, so a documentation checker does not
  become a coverage obligation.
- **The benchmark does not ship in this branch.** Revision 2 had a present-tense sentence implying
  the graph-shape script and fixtures were committed. They are not, and they still are not: §2 and
  §20.3 are future-tense Stage 2 and Stage 8 commitments, and say the figures are reproducible in
  principle and not yet from this repository.

### The retired-phrase check, and why it needs exclusions

Review 02 noted that a whole-document prohibition on withdrawn claims would reject the document's own
explanation of them. That is structural rather than a matter of care: withdrawn claims are discussed
*here*, in a document the reference's checker does not police, and the checker splits its prohibition
in two — claims that must appear nowhere at all, and component names that may appear only in sections
that exist to discuss history.

## Review 03 — disposition

[`parallel-execution-review-03.md`](parallel-execution-review-03.md), assessing `fd24db1`.
Assessed against revision 3; answered in revision 4. Seventeen principal findings — ten P1, seven P2 — and thirty-one smaller consistency issues.

**Every finding was evaluated against source before disposition, and all seventeen hold.** Three
recommendations are modified rather than adopted as written, and each modification is argued below the
table with what was checked. Nothing is dismissed; nothing is silently omitted.

| ID | Finding | Disposition in revision 4 |
|---|---|---|
| R3-01 | A failed target claim still permits a competing writer | **Accepted.** Revision 3 fell back to "capacity one" when the target claim could not be acquired, which is exactly the conflicting inline execution the claim existed to prevent. §8.5 puts one registry at `<workspace-root>/.execution-registry/`, states that **every executor acquires from it with no exemption for inline**, and makes failure to acquire a refusal to start *that task* rather than a mode change. §17's degraded-host fallback is now explicitly about missing worker capability only, never about a refused grant. |
| R3-02 | Ownership checking and canonical mutation have a fencing race | **Accepted.** `ownership_generation` is canonical rather than a `runtime.json` field (§5.3, §5.5), takeover is itself a revision-checked commit that raises it (§12.1), and every subsequent canonical mutation carries the generation it read *inside the same candidate*, so a superseded coordinator loses on the revision check instead of winning a check-then-write race (§12.2). Physical dispatch is fenced separately: the generation is re-read between the `start-permit` and the host call, and a raised generation aborts the launch (§12.3). |
| R3-03 | Coarse repository claims do not exclude ordinary path writers | **Accepted.** The `exclusive` namespace — the thing an ordinary `path` claim could not see — is deleted. §8.2 has two namespaces, `path` and `external`, and one **symmetric** ancestry predicate, so `path:/repo` write conflicts with `path:/repo/b.py` write in both directions. Verification no longer conflicts with itself: a coordinator re-run executes under its own attempt's existing grant and requests no new claim (§8.2, §11.4). |
| R3-04 | Recomputing the contract can reject a task's intended edit | **Accepted.** §6.6 splits identity into three sets: `definition` (what the task is), `baseline` (the frozen inputs and the pre-state of `writable_subjects`), and `produced` (what the writer wrote). `definition_hash` covers only the first. A declared output is a `writable_subject`, so its bytes changing is the expected outcome and invalidates nothing; `hold/contract_invalidated` (T25) fires only on a `definition` change before the permit was written. |
| R3-05 | T21 uses a nonexistent authorization status and omits the no-authorization case | **Accepted; the nonexistent status is a real defect.** `AUTHORIZATION_STATUSES` is `{not_required, pending, explicit, denied, deferred}` [C17]. **Corrected 2026-09-11 after review 04:** revision 3's T21 said the status was no longer `authorized`, with hold reason `authorization_revoked`; it did not use `revoked`, and this row and revision 4's model test both said it did. `git show fd24db1` is the source. The finding is unaffected — `authorized` is not in the enum either — but the regression test was testing a string revision 3 never contained, and §21.2's replacement tests the real one. §8.1 conditions 3 and 4 are two named predicates — `delegable` and `in_force` — over the real enum, with `pending`, `denied` and `deferred` all not in force and reported distinctly. §14.1 makes withdrawal **one** canonical commit setting `RUNNING → BLOCKED` and `authorization.status: denied` together, both legal in one candidate. |
| R3-06 | Quarantine is terminal for scheduling but may still contain a live writer | **Accepted.** Claim-holding is decoupled from the label entirely: an attempt holds its grants iff `prepared.json` is present and `release.json` is absent (§5.2), a pure function of the record set. `QUARANTINED`, `UNCERTAIN` and `STOPPED` all still appear in resource accounting, and `release` requires one of §13.2's five stop-evidence kinds — "a bare pid is not evidence". |
| R3-07 | The transition table still has uncovered cross-file crash windows | **Accepted.** §6.3 fixes the dispatch ordering as `canonical RUNNING commit → start-permit → re-read generation → host call → launch` and enumerates every prefix with exactly one recovery outcome, each idempotent. Exactly one prefix — permit written, no launch record — cannot be resolved from records, and it resolves to `UNCERTAIN` (T5) rather than to a guess. |
| R3-08 | Malformed publications and pending failures can evade the dispatch pause | **Accepted.** There is no durable pause flag to evade: §7.3's `dispatch_blocked` is a predicate over the journal, and §5.3 states that `runtime.json` is a cache never read to make a decision. An unresolved quarantine hold or an unclassified published failure blocks new dispatch because the predicate reads the same record that caused it. |
| R3-09 | Verification inference can silently weaken the required check | **Accepted, and the reviewer's tokenization result was reproduced.** §11.2 resolves `verification` strings into `checks` **at planning time**, records `checks_interpretation` with the source text and the resolution, and **rejects** at admission any string that is not a single executable command. Shell composition is either split into several checks or declared explicitly as `["bash","-lc",…]` with `shell: true` and authorization for it. Revision 3's silent fall back to `method: inspection` is named in the reference as the failure mode the section exists to prevent. |
| R3-10 | The capture schema cannot represent the specified evidence model | **Accepted.** §11.3 is one record tagged by `record_kind`. `command_run` requires `argv`, `cwd`, `exit_status`, `duration_ms` and `streams` and forbids the assessment fields; `assessment` requires `criteria`, `rationale` and `assessor` and forbids the command fields. `exit_status` is `{kind, code | signal}` over `{exited, signalled, timeout, not_run}`, so a timed-out check no longer needs a fictional exit code. Contradictory combinations are unrepresentable rather than discouraged. |
| R3-11 | Receipt timestamps and missing lookup rules still make retries non-idempotent | **Accepted.** §7.5: `receipt_id` is the digest of the `acceptance` record with `receipt` omitted, so it is a function of content and not of when it was written; `accepted_at` is reused **verbatim** from the existing record on any retry; `observed_revision` is documented as an upper bound, not an identity. A retried acceptance is therefore a byte-identical publish-if-absent, which succeeds and appends no second evidence entry. |
| R3-12 | The schema-version gate requires a migration and reader rollout design | **Accepted.** §5.5 defines `schema_version: 4` with its `execution` block and a dedicated `research-project enable-execution <project-dir> --expected-revision <rev>`. The dedicated command is not a convenience: `schema_version` is in `IMMUTABLE_PROJECT_FIELDS` [C21], so an ordinary commit cannot change it. §19 makes the 4→6→7 stage order a correctness requirement, and mixed-version refusal is verified on all three hosts. |
| R3-13 | Multi-check verification, failures, and coordinator reruns are not represented coherently | **Accepted.** Adequacy is decided **per `check_id`** (§11.5). Coordinator re-runs publish into `captures/coordinator/<check-id>-<seq>` and never modify a worker's captures (§11.4). `acceptance.qualifying_captures` names one capture per check and `superseded_captures` retains the rest, so a failed-then-passing history is visible rather than erased. A `valid_failed` result with complete captures is *adequate* and is integrated as canonical `BLOCKED`. |
| R3-14 | Publication immutability needs a publish-if-absent operation, not ordinary replacement | **Accepted; this reverses part of revision 3's answer to review 02 finding 3.** §7.1 is `O_CREAT|O_EXCL` → write → `fsync` → `os.link` to the final name → unlink tmp → `fsync` dir, with a byte comparison on `FileExistsError`: identical bytes are success, differing bytes are `hold/conflicting_publication` (T12). `atomic_link` is listed as a §17 host prerequisite, with the NFS caveat stated rather than assumed away. |
| R3-15 | Worktrees are marked available without an integration protocol | **Accepted; deferred rather than designed.** §9 ships `shared` only. §21 lists `worktree` and `copy` with the specific missing pieces: merge or rebase rules, conflict ownership, `.git` claim semantics across worktrees, and partial-merge recovery. |
| R3-16 | The decision history describes a different revision 1 and a different review 01 | **Accepted; verified independently, and the fabrications are deleted rather than reworded.** `git show e18e589:…` confirms all four rows of the review's contradiction table. Two sections are removed, the revision-1 row is rewritten from the commit, and all nine review-01 findings are restored with their real subjects and priorities. Two things go beyond the review: revision 1's actual defect — the projected `.project.lock` contention — is recorded with the source evidence that falsifies it, and a further error the review did not name is corrected (revision 3 counted one medium review-01 finding; there are three). No motive is attributed to the historical author. |
| R3-17 | The documentation checker does not check several structural guarantees it advertises | **Accepted.** `tests/plugins/research/test_parallel_execution_doc.py` is rewritten around relationships rather than phrases: citation needles re-read from source, every `§`/`[Cn]`/`Tn`/`Un` reference resolved to a definition, Markdown links resolved on disk, the label table checked against its own prose count and its uses, and **every canonical cell in §6.2 checked against `TASK_STATUSES` and `TASK_TRANSITIONS` read out of `workspace_lib.py`**. The constants are parsed from the source with `ast` rather than imported, so a prose checker stays outside the coverage gate that guards the shipped scripts. The three in-memory mutations the review reported passing now fail. §20.1 is narrowed to what a checker can actually enforce; §20.2 is the model test and states what it does not establish. |

### Three recommendations modified rather than adopted as written

1. **R3-01's registry is workspace-scoped, not machine-scoped (and S15 asks for exactly this).** The
   review's failure trace is about two projects in one workspace targeting one repository, and a
   workspace-scoped registry closes it. A machine-scoped registry would also close the case of two
   *workspaces* targeting one repository, but a second workspace root on one machine is already a
   defect the skill tells you to report rather than a configuration to support, and discovering other
   workspaces is a new mechanism with its own failure modes. So §8.5 is workspace-scoped, states that
   scope as a limitation in the same section, and §21 lists cross-workspace coordination as deferred
   with what it would need.

2. **R3-06's stop evidence is a closed set of five kinds, not a general "explicit resource check".**
   S23 makes the same point from the other side, and it is the stronger reading: absence of a
   currently visible write is not evidence that nothing will write later. §13.2 therefore enumerates
   what counts — and a bare pid, a stale heartbeat and a quiet filesystem are all excluded. Where no
   listed kind is available, the attempt **retains** `UNCERTAIN` and its claims indefinitely and an
   operator resolves it. That is worse ergonomics than a heuristic and it is the only honest option on
   a host with no `discover` capability, which is the host this design actually runs on (§17).

3. **R3-09's rejection is at admission, not at runtime.** The review asked that inference not weaken a
   check. Rejecting a shell-composed string *when the check runs* would fail the task after the work
   is done; rejecting it at admission fails the plan before anything is dispatched, names the offending
   text, and leaves the two legitimate forms (split the check, or declare the shell explicitly with
   authorization) available to the author. This is a change of timing, not of verdict.

### The thirty-one smaller issues

| ID | Disposition in revision 4 |
|---|---|
| S01 | **Accepted.** §6.1 is a 14-row first-match table yielding 14 labels, one row each, and the count is stated in its prose; claim-holding and finalization are stated as two separate predicates rather than folded into the row conditions, which is what the revision-4 draft got wrong. **Corrected 2026-09-11 after review 04:** this row credited revision 3 with the presence-derived label table; `git show fd24db1:…` shows a mutable `state.json` lifecycle and transitions T1–T25 only, so the folded predicates were a defect of an intermediate revision-4 draft rather than of committed revision 3. Quarantine is terminal for *scheduling* and not for claim-holding, which is the distinction that made "is quarantine terminal?" unanswerable; §5.2 states it. The checker derives its label set from the table, checks the table is a bijection, and compares it against both the prose count and the labels §6.2 uses, instead of hardcoding a count. |
| S02 | **Accepted.** The dangling `§14.4` is gone. **Corrected 2026-09-11 after review 04:** revision 4 does have `### 14.1`, so the claim that §14 had no subsections was false; what is true, and what closed the finding, is that no `§14.<n>` reference dangles. The review-02 disposition above says "step 4 of §14", and the checker resolves every `§` reference to a heading, reporting zero unresolved. |
| S03 | **Accepted.** The instruction that `external` references become absolute paths is deleted. §8.2 gives `external` a stable opaque name declared by the plan, and the reference contains no surviving absolute-path rule for it. |
| S04 | **Accepted.** Receipt and transitive evidence-reference validation appear once, in Stage 4 (§19). Stage 3 is the registry. |
| S05 | **Accepted, by deleting the field rather than marking it optional.** There is no `handle` field whose absence must be distinguished from null: the `launch` record's presence *is* the signal, and §5.2 answers every state question from file presence. |
| S06 | **Accepted.** The pause object is gone (§7.3 is a predicate). `deadline_ms` and `heartbeat_interval_ms` are required integers in §6.5, and §13.2 derives `heartbeat_bound_ms = max(90000, 3 × heartbeat_interval_ms)`, so there is no absent-interval case to give a meaning to. Before the first heartbeat the bound runs from the `launch` timestamp. |
| S07 | **Accepted.** §5.4 is one encoding applied to all three content-addressed records. **Numbers are integers or strings only**, which removes shortest-round-trip, negative-zero and non-finite questions rather than answering them; duplicate keys are a parse error; absent optionals are omitted so "absent" and "null" cannot hash alike. |
| S08 | **Accepted.** §5.4 has an id-syntax table with path-safe patterns for every id kind and the digest format `sha256:<64 hex>`; §6.5 has a nested-requiredness paragraph covering arrays, conditional fields and the closed capability set. An id that does not match its own directory name makes the record unreadable. |
| S09 | **Accepted by restriction rather than by definition.** §6.6 hashes regular files only: a symlink component is a derivation error, directories are not hashed, external artifacts are not hashed, and an input set that cannot be enumerated records `inputs_enumerated: false`. Tree digests are in §21 with what they would need. |
| S10 | **Accepted.** §6.6 states the coordinator's explicit declaration step and the conservative fallback. Nothing infers a read set from a verification argv. |
| S11 | **Accepted with the conservative option.** A symlink component in any declared path is refused at derivation and at admission, so there is no resolve-once-then-retarget window. Hardlink aliasing is **not** detected, and §6.6 says so in the same paragraph, with §21 carrying what detection would need. |
| S12 | **Accepted.** §5.3 lists the case-sensitivity probe results as a `runtime.json` field. The "record both forms" instruction went with the absolute-path rule (S03), so there is no unrecorded form left to describe. |
| S13 | **Accepted.** §6.6 probes **each distinct volume once per run** and uses that volume's result, rather than one runtime-wide answer. C13 is no longer cited in support of filesystem semantics. |
| S14 | **Accepted.** §8.2 makes a bare name a derivation error that names the offending value, and every `path` claim an absolute resolved path. Ancestry is symmetric, so a parent write claim conflicts with any protected descendant automatically. Coordinator bookkeeping is a claim like any other — `path:<project-dir>/project.json` write, held by the coordinator and never granted to a worker — which also covers a legitimate coordinator-owned task editing a shared record. |
| S15 | **Accepted.** The registry is at the **workspace root**, not per target, and holds `path` grants for every root including `workspace_root`, so two projects with different targets still conflict on a shared `memory/<slug>.md`. The guarantee is explicitly scoped to one workspace root, and unrelated roots are named as out of scope (§21). |
| S16 | **Accepted.** §8.1 states that admission requires `TODO` with **no** normalization, because a candidate that quietly rewrote `BLOCKED` to `TODO` would erase the reason. Retry is a separate canonical `BLOCKED → TODO` commit carrying the previous block reason forward, legal under `TASK_TRANSITIONS` [C20]; T22's `RUNNING → TODO` is the same rule from the other side. |
| S17 | **Accepted.** The hold rows are written over label ranges rather than hand-listed subsets: T26 and T27 span `PREPARED`…`RUNNING`, and T28 (`superseded`) and T29 span **any unresolved** label. A valid result from cancelled work is still recorded. **Corrected 2026-09-11 after review 04:** revision 4's §14.1 does not mention checks at all, so this row's second clause described text that was not there; revision 5 states the prohibition where it belongs, in §16.4's worker contract. |
| S18 | **Accepted.** §14.1 separates three things that revision 3 collapsed: an execution failure, an operator stop request (`hold/stop_requested`, explicitly not a failure), and withdrawn authority (`hold/authorization_withdrawn`, one canonical commit with `denied`). Each makes a best effort to stop the worker and, where the host cannot, retains uncertainty and reports what could not be stopped rather than asserting it stopped. |
| S19 | **Accepted with the claim narrowed to what it proves.** §11.3 records `subject_digests_before` and `subject_digests_after`, and states in the same paragraph that equality shows the subject did not change *across* the check and **does not** show it was unchanged *during* the check — a check that writes a file and restores it looks identical. The stronger claim revision 3 made is withdrawn. |
| S20 | **Accepted.** A resolved check carries `subjects` and `independence` (§11.2), and §11.5 compares both per `check_id`. **Corrected 2026-09-11 after review 04:** revision 4's §8.1 had no such condition, so "rejected at admission" was a claim about text that did not exist. Revision 5 adds it as clause 8 of §10.1, refusing `R-CHECK-UNCOVERED`, and §11.4 states that a task with no checks cannot reach `DONE`. |
| S21 | **Accepted.** §17's `spawn` returns `started(handle)`, `definitively_not_started`, or `ambiguous`; `discover` returns `running`, `never_started`, or `unknown` as three distinct results with distinct consequences; `launch_key` is an input to `spawn` and is recorded in `start-permit`. |
| S22 | **Accepted, by declaring the inline adapter's actual guarantees instead of promising equivalence.** §17 states that inline `spawn` runs synchronously and therefore never returns `ambiguous` — so inline has no dispatch-ambiguity window at all — that `discover` always returns `finished`, that `interrupt` is unavailable, and that `bounded_context` is false. No claim is made about the observability of subprocess descendants. |
| S23 | **Accepted.** See the second modified recommendation above: §13.2's five stop-evidence kinds are a closed set, and the absence of a visible write is not one of them. |
| S24 | **Accepted, and the unsupported claim is withdrawn.** The reference no longer says existing validation already catches a removed capture. Transitive evidence-reference validation is explicit Stage 4 work (§19), and §5.7's retention rule no longer leans on it. |
| S25 | **Accepted.** §5.7 requires a durable `collection` tombstone naming the files *before* they are removed: a missing file named by a tombstone is collected, a missing file with no tombstone is an error that stops dispatch until an operator reconciles it, and a crash between tombstone and deletion is re-run idempotently (§6.3). Removing an empty lock directory in normal operation is distinguished from collection under `execution/`. |
| S26 | **Accepted.** `max_log_bytes` is a required contract field (§6.5) and the capture record carries `truncated` per stream. §5.7 applies the skill's existing redaction rule before the bytes are written, so the digest covers the redacted content and there is no second unredacted artifact to protect. |
| S27 | **Accepted.** The reference is self-contained: its preamble states that `docs/` is not in the installed plugin subtree, no normative rule depends on this document, and there is no cross-subtree link a shipped plugin would follow. |
| S28 | **Accepted.** §22 was rebuilt from source rather than repaired: every needle was re-read at its stated line range, the two misattributed citations were replaced with the code they actually support, and the ids were renumbered contiguously with every inline reference remapped. **Corrected 2026-09-11 after review 04:** revision 3's checked-in table had **twenty-six** rows, not twenty-one, and the count of drifted rows was derived from the wrong total, so it is withdrawn rather than restated: what was re-established is that every row of the rebuilt table resolves, which is what the checker tests. The rebuild is why the rebuild is why the checker's citation test is worth running. |
| S29 | **Accepted.** The nine conditions from revision 2 are restored verbatim in substance below, under "Conditions for reconsidering a database and an MCP transport". **Corrected 2026-09-11 after review 04:** revision 4's §19 contained no reassessment stage — its last row promised reporting and the benchmark only — so the cross-reference pointed at nothing. Revision 5's Stage 8 (§20) names the reassessment against these conditions explicitly. |
| S30 | **Accepted.** §1 qualifies the context claim by host capability: bounded worker context is a host capability (`bounded_context`), it is false for the inline adapter, and the claim is stated for hosts that assert it rather than unconditionally. |
| S31 | **Accepted.** §8.4 defines the ceiling as **dispatched workers**: inline execution does not consume a slot but does hold claims, and non-terminal attempts and held grants are counted by the resource rule instead. §16 describes graph width separately from worker count, and no five-wide claim survives against a ceiling of four. |

## Review 04 — disposition

[`parallel-execution-review-04.md`](parallel-execution-review-04.md), assessing `812010c`.
Assessed against revision 4; answered in revision 5. Eighteen principal findings — fourteen P1, four
P2 — and twenty-eight smaller details.

**Every finding was checked against source before disposition, and all eighteen hold.** Nine
read-only probes the review reports were re-run and all nine reproduce; the seven historical claims it
falsifies were re-read from the commits and all seven were wrong, and are corrected in place above and
listed below. Two findings I expected to over-reach — S4-17's symmetry claim and S4-26's marker
count — were correct in substance. One finding is answered with **stronger** evidence than the review
used, which makes a different finding worse: see below the tables.

This is the revision where the answer to a finding is often a refusal rather than a mechanism. Roughly
half of revision 4's promises are demoted: §3.1 lists the nine task shapes v1 supports, §4 gives
thirteen refusal codes for everything outside them, and §22 lists what was deferred with what it would
need. A refusal that names its code and its one operator action is a smaller claim than a mechanism,
and it is one the model can check.

| ID | Finding | Disposition in revision 5 |
|---|---|---|
| R4-01 | A delayed physical launch can outlive the reservation protecting it | **Accepted.** The launch capability is a field on the grant, not a separate permit: `capability` moves `none → issued → consumed` (§6.1), and `issued` means a start may already have happened. O16 cannot release a grant whose capability is `issued`, takeover cannot remove it without a proof of death *and* an explicit `revoked` under the registry lock (§12.2), and §8.2's ambiguity rule forbids re-issuing it. The reservation therefore cannot end while a launch it authorized might be in flight; I2 states that as an invariant and §21.2 checks it under crash injection. |
| R4-02 | Canonical fencing does not fence journal writes or coordinator checks | **Accepted.** §12.4 is new and fences journal writes separately from canonical ones, asymmetrically and on purpose: publish-if-absent never clobbers, so a superseded coordinator's record is byte-identical (harmless), or a recorded `conflicting_publication`, or — where its generation cannot be explained from recorded takeovers — an `indeterminate` projection that stops the reader (§6.3, I5). Coordinator-executed checks write under the attempt's own grant and carry the same generation. What is *not* prevented is a ghost writing at all, or a second coordinator being started; that is U4, marked at its site. |
| R4-03 | "Stopped" and the inline fallback do not establish absence of future writes | **Accepted, and the situation is worse than the review argued.** Detached and resumable execution are outside the envelope entirely (`R-DETACHED-CHILD`, §3.1), so the only workers admitted are ones an adapter can attest are gone. §17.1 records first-hand evidence from this host's own tool surface that a *completed* subagent is resumable, which means "finished" is not "sealed" for that adapter — evidence the review's cited documentation does not carry. Sealing therefore requires an adapter attestation or one of §13.1's stop-evidence kinds; where neither exists the attempt retains its scopes indefinitely and an operator resolves it. The inline fallback makes no absence-of-writes claim. |
| R4-04 | Acquisition has an orphan prefix, and release has two opposite orders | **Accepted.** Every operation in §8.1 carries one ordered list of durable effects in its own column, and §8.2 enumerates every prefix of every operation with exactly one completion. Acquisition is O1 `reserve` then O2 `grant`, each idempotent on replay against its own record, so the orphan prefix completes rather than leaking. Release is O16 alone; revision 4's two release orders were two sections describing one operation, and there is now one. |
| R4-05 | The label graph excludes real events and omits necessary transition guards | **Accepted, by deleting the graph.** There is no label graph to complete or guard. §6.1 is a set of facts, each with a named source; §6.2 derives one display label by first match and is total by construction, its last row matching anything else. Guards are preconditions over facts (§8.1), never over labels, and §21.1's checker fails any precondition stated over a label of §6.2 that is not also a canonical task status. An event the label vocabulary does not name can no longer be an unreachable state, because nothing reads the label to decide. |
| R4-06 | Failure classification clears the dispatch gate too early | **Accepted.** §14.2: the gate closes on classification (O7) and opens on resolution (O9). Revision 4's two contradictory sentences are quoted there as the defect the section exists to fix — a gate that opens on classification opens as soon as the coordinator has described the failure to itself, which is not a decision anyone made. `operator_stop` causes are excluded from the gate condition, because a stop is an instruction being carried out rather than a diagnosis awaiting one. |
| R4-07 | Adequacy no longer binds successful evidence to accepted output bytes | **Accepted.** §11.3's `accepted_snapshot` is computed at acceptance over every declared write claim and every check subject, and O11 requires each qualifying capture's `subjects[].after` to equal the corresponding entry. A file that changed after its check ran raises `stale_evidence` and the check reruns; it is not accepted. §11.3 also states, in the same place, what this does *not* show — a check that writes a file and restores it is indistinguishable — so the binding is across the check, never during it. I3 states it and §21.2 checks it. |
| R4-08 | Revalidation protects only preparation, not acceptance against a changed plan | **Accepted.** `accept` re-derives `definition_hash` from the *current* `project.json` and refuses on mismatch with a `definition_changed` cause, withdrawing the attempt via O13: it verified a task that no longer exists. Nothing prevents the edit, because the definition lives in a file a person may change at any time, so §11.3 marks this U3 at its site — detection at the last safe moment, not prevention. |
| R4-09 | Correct claim comparison cannot repair incomplete claim derivation | **Accepted, by refusing what cannot be derived.** The weak half was never the comparison. §3.1 admits only task shapes whose subjects are exhaustively enumerable from the plan: a directory subject is `R-DIRECTORY-SUBJECT`, an unenumerable write set is `R-UNENUMERABLE`, and a reference outside the workspace is `R-EXTERNAL-REFERENCE`. §9.1's derivation is total over what remains and §9.3's relation is symmetric over that. Tree digests and hardlink aliasing stay deferred (§22) with what they would need. |
| R4-10 | The proposed canonical mutations do not satisfy the real validator | **Accepted; reproduced against the shipped validator.** §8.4 states the rules revision 4's mutations broke — `current_tasks` equal to the exact set of `RUNNING` ids [C1], `BLOCKED` requiring `block_reason` [C25], explicit authorization required for the statuses that demand it [C18], immutable identity preserved [C28] — and every operation's canonical cell in §8.1 names a transition `TASK_TRANSITIONS` permits. §21.2 does not assert legality: it builds the real candidate and dry-runs it through the real validator. |
| R4-11 | Planning-time checks and several supported task shapes lack a storage contract | **Accepted.** §11.1 resolves each `verification` string at planning time into a `checks[]` entry carrying `argv`, `subjects`, `expect_exit` and the permitted assessor, stored in the execution plan (§9.1) rather than recomputed per attempt, and §11.3 replaces revision 4's free verdict with the expected-exit policy. The task shapes that had no storage contract are the ones §3.1 now refuses by code rather than describing. |
| R4-12 | Publish-if-absent still leaves durability and conflicting-publication gaps | **Accepted.** §7.2 is one primitive: temp file in the same directory as its target, so `os.link` is same-volume by construction; short writes retried to completion; `fsync` on file and directory; byte comparison on `FileExistsError`, where identical is success and different is a `conflicting_publication` cause. §7.5 gives three durability classes and says which records are D1. A host without atomic-link semantics is `R-NO-ATOMIC-LINK` and a cross-volume store is `R-CROSS-VOLUME`. The residual the review is right about — that a conflicting publication is an observed event, not a property of the final bytes — is stated as U5 at its site. |
| R4-13 | The version gate does not exclude legacy writers in other projects | **Accepted, and narrowed to what a version field can do.** §7.6 states plainly that a per-project gate cannot exclude a legacy writer in a different project, because that writer never reads this `project.json`. What a cross-project conflict actually meets is the workspace registry (§10.4), and a coordinator that finds a grant it cannot interpret refuses with `R-LEGACY-WRITER` rather than proceeding on the assumption that it is alone. U6 marks what the gate does not enforce. |
| R4-14 | Canonical receipt lookup and finalization need identity checks, not status equality | **Accepted.** §11.3: the receipt is `rcp-` plus 32 hex of the digest of `{accepted_snapshot, attempt_id, definition_hash, intent, qualifying_captures}`, and it is the only proof a commit landed (I4). `commit-observed` records a re-read of `project.json` that found *that* receipt in the task's `evidence` array. Nothing infers a commit from a status, and the reference says so from the other side: a `DONE` task whose evidence lacks the receipt is an acceptance that did not land, whatever its status says. |
| R4-15 | The model's strongest advertised guarantees are not actually tested | **Accepted; the model was replaced rather than extended.** §21.2 is a small stateful model — two coordinators, two tasks, one shared write path, one checker, one launch capability — with canonical state and external execution modelled separately, recovery expressed as a function from store to operations whose durable effects are then applied, and crashes injected at every prefix of every operation. Assertions are I1–I5 stated externally over the modelled world, not over label names. Every trace in this review is a named regression case. Revision 4's `apply_outcome` discarded the crash prefix it was handed, its no-redispatch test compared strings, and its `fields_ok` forbade the other record kind's fields without ever requiring its own; all three are deleted rather than repaired. |
| R4-16 | Host claims need versioned evidence, and fallback errors need classification | **Accepted, with one claim now resting on better evidence than the review's own source.** §17.1's matrix records, per capability, the evidence it rests on; for this host that evidence is its own tool contract as observed in the session that wrote the section, which is first-hand rather than documentary. Adapter failures classify into §14.1's fourteen causes instead of collapsing into one error, and a capability that cannot be evidenced yields `R-NO-ADAPTER` rather than a degraded mode. |
| R4-17 | The decision record again overstates what changed and what the checks establish | **Accepted in full.** All seven falsified historical claims were re-read from the commits, all seven were wrong, and each is corrected at its own row above with what was checked and what is withdrawn rather than restated; they are listed together below. §21.1's promise list is replaced by eleven statements of exactly what the checker relates, and the checker now parses every canonical cell completely so a cell of unrelated prose fails, resolves link fragments to headings in the target file, and tracks review coverage through the assessed/answered revision pair each disposition section records — which is what makes this section's own honesty testable rather than asserted. |
| R4-18 | The efficient implementation path needs a smaller state core and bounded work | **Accepted.** §10.2 is an explicit ready loop: settling before starting, plan order as the only tie-break with fairness as a consequence, `max_prepare` bounding in-flight preparations at 2, and a stated rule for a result arriving between the gate scan and the launch. §15.2 does the arithmetic the review asked for — 8 heartbeat files a minute, 11,520 a day — then makes heartbeats D2 and reapable, so four exist in steady state and none is in the admission path. `runtime.json` is a discardable mirror; the probe results a derivation actually reads live in `config.json` (§15.1), which resolves revision 4's contradiction in the direction the review recommended. §20 is eight stages with the two behaviour-changing ones named as such, and §21.3 states what the benchmark must control before any default can be called economical. |

### Where this revision goes beyond the review, and where it refuses instead

1. **R4-16 is answered with evidence that makes R4-03 worse.** The review argued that host capability
   claims need versioned evidence, citing subagent documentation. The running host's own tool contract
   is stronger evidence than documentation about it, and what it shows is unfavourable: a stop tool
   exists, agent listings distinguish busy from idle, and a message to a *completed* agent resumes it
   from its transcript. A completed agent is therefore resumable, so adapter completion is not
   sealing, and §17.1 records that as first-hand evidence rather than citing a page. §3.1's
   `R-DETACHED-CHILD` refusal exists because of it.

2. **Three findings are answered by narrowing the envelope rather than by building the mechanism they
   imply.** R4-03 (absence of future writes), R4-09 (complete claim derivation) and R4-13
   (cross-project exclusion) all describe mechanisms that would need capabilities this design does not
   have. Each is answered with a refusal code, a named operator action, and a §22 row saying what
   building it would require. This is a smaller design than revision 4 promised, and the smallness is
   the point: an ambiguous promise about a live writer is worse than a refusal that names the
   predecessor's pid.

3. **The model was replaced, not repaired, because its failures were structural.** A suite of 1,208
   subtests over label combinations cannot find a crash-ordering defect, because it never models a
   crash. §21.2 states what the new model does not establish, in the same section, so a green model run
   is not read as a correctness proof.

### The twenty-eight smaller details

| ID | Disposition in revision 5 |
|---|---|
| S4-01 | **Accepted.** The "two predicates, three questions" text is gone. §6.1 is a fact set whose sources are named per fact, §6.3 says why absence is not a fact, and projections read **validated** records: a record that fails its schema or carries an unexplained generation sets `indeterminate` (I5) rather than being counted as present. |
| S4-02 | **Accepted.** §5's diagram uses the schema's own names — `ownership_generation` and `coordinator_run` — and the `execution` block is drawn with the fields §7.6 defines. |
| S4-03 | **Accepted.** There is one receipt rule, in §11.3: the digest is over the five-key object with `receipt` itself absent from the input, so there is no self-reference. §7.3 no longer states a second, different rule. |
| S4-04 | **Accepted.** §7.3 fixes exactly one encoding: no trailing newline, keys sorted by code point, arrays in document order, duplicate keys a parse error, integers and strings only, and invalid Unicode including lone surrogates rejected at parse rather than left to a runtime encoder. |
| S4-05 | **Accepted.** Captures are no longer called content-addressed. `capture_id` is an opaque id with a stated pattern, the content digest is a named field on the capture, and §11.3's receipt names `capture_id`s rather than digests, so no field is inside its own hash. |
| S4-06 | **Accepted.** §7.3 separates the JSON value from the filename encoding: `sequence` is an unbounded integer in the record and a four-digit padded field in the filename, and §11.4's rerun cap of 16 makes the padded form unreachable at its bound. Heartbeats are per attempt and carry the writer's `coordinator_run`, so there is no shared sequence namespace. |
| S4-07 | **Accepted.** §7.4 gives a full field table per record kind rather than a one-line "carries" summary, including the `launch` timestamp R4/S4-24 needs, stop-evidence identity, hold detail, resolution `decided_at`/`decided_by`, and release preconditions. |
| S4-08 | **Accepted.** §7.6's unknown-version rule covers unknown record kinds, a wrong JSON top-level type, boolean-for-integer, and malformed nested fields, all as `corrupt_record`; and it states that unknown minor-version fields are included in a content hash before being ignored, so a digest cannot be forged by adding a field a reader skips. |
| S4-09 | **Accepted.** §7.6's activation command is specified as one already-enabled no-op: the expected revision is checked *first*, so a stale caller is told so rather than silently succeeding, and the no-op path states what it writes (nothing) and what it returns. The required null coordinator value and protocol-version validation are both listed. |
| S4-10 | **Accepted.** §15.1 gives every duration a positive bound and a unit, §9.1 records the deadline as a UTC instant with monotonic elapsed accounting inside a process, and §13.2 states the restart treatment: elapsed time is recomputed from the durable instant, not carried in memory. |
| S4-11 | **Accepted.** §9.4 lists the specific inputs a baseline does not cover and why, rather than one global boolean, and removes from the read-only baseline any dependency output this task also declares as writable — which is exactly the case revision 4 double-counted. |
| S4-12 | **Accepted.** §9.4 probes case sensitivity once per volume inside the execution store's own directory, never in the target, records the result in `config.json`, and states how comparison uses it. Admission-time symlink rejection is stated as *not* preventing later retargeting by an outside actor; that residual is U1's territory. |
| S4-13 | **Accepted.** §13.3 uses a tagged outcome per declared output — `present`, `absent`, `changed_type`, `unreadable`, `unsupported` — instead of three assumed digest cases, and each tag has one consequence. A directory output is `R-DIRECTORY-SUBJECT` at admission, so it cannot reach reconciliation. |
| S4-14 | **Accepted.** §7.2 no longer says "prepared plus its logs". It constrains temp and final to one directory, handles short writes, and normalizes I/O failures into named causes. |
| S4-15 | **Accepted.** §7.2 places the classifier: a missing capture file, an invalid capture schema and an unrecognized outcome value are each a named cause, and conflicting publication is an *observed event* recorded when the primitive returns `conflict`, not derived from the final manifest. U5 marks the limit. |
| S4-16 | **Accepted.** §10.1's clauses are universal admission; executor eligibility is separate, so nondelegable work runs inline without the admission clause claiming to permit it. Clause 1 requires the project to be `EXECUTING`, not merely the task `TODO`. |
| S4-17 | **Accepted; the review is right and the old wording was self-refuting.** §9.3 says the conflict relation is **symmetric**, and its examples now read as symmetry demonstrations rather than as counter-examples to the sentence above them. |
| S4-18 | **Accepted; this disposition was wrong and is corrected 2026-09-11 after review 05.** It credited revision 5 with escaping rules for `external` claim keys. External claims are *refused*: `local` and `store` are the only namespaces §9.2 admits, so there are no external keys to escape and no two-spellings limitation to state. What revision 5 actually did was narrow the namespace; the escaping was removed with it. |
| S4-19 | **Accepted.** §9.2 and §11.1 separate executor permission from attempt-owned reservation: all checks for an attempt are covered by that attempt's single grant, and which executor runs a check is a selection over what the grant already holds. A coordinator check needs no second grant. |
| S4-20 | **Accepted.** §16.2 gives the attempt-owned store namespace explicitly — the attempt's own directory under `execution/`, for captures, result and heartbeats — with its limits, instead of telling workers to write outside their literal claim set. |
| S4-21 | **Accepted, with only the semantics-preserving split offered.** §11.1 splits nothing conditional: `&&`, `||` and pipelines are refused unless declared as an explicit shell invocation with authorization, and a quoted literal `;` or `|` inside an argument is not treated as composition. Revision 4's "split into several unconditional checks" advice is withdrawn, because it changes what the check means. |
| S4-22 | **Accepted.** §11.2 forbids the actual field names — `criteria`, `rationale`, `assessor` — on a command capture, and the `exit` variants are a closed set (`exited`, `signalled`, `timeout`, `spawn_failed`, `unknown`) with `code` present only for `exited` and `signal` only for `signalled`. |
| S4-23 | **Accepted.** §11.5 checks compatibility before precedence: kind, subject identity, actor relation and executor must match, and only then does owner precedence apply — so a coordinator assessment cannot replace a required human one. §8.3 states that repeated execution is idempotent for the *journal*, and explicitly not for a task's side effects. |
| S4-24 | **Accepted.** The `launch` record carries a required timestamp (§7.4) and §13.2's first-heartbeat bound runs from it. §12.1 gives the coordinator its own `owner` record with a heartbeat, so the same staleness bound applies to the owner — though staleness is now only a takeover *trigger*, never a proof (§12.2). |
| S4-25 | **Accepted; this was the review's most concrete correction and it is now a test.** Both lock timeouts in §15.1 are **5.0 s**, matching the shipped `DirectoryLock.__init__` and `commit_candidate` defaults [C24][C29], and §21.1's checker reads those two defaults out of the source with `ast` and fails if the table disagrees. Every setting lives in `config.json`, whose location and durability §15.1 states. |
| S4-26 | **Accepted, and the rule count came down with it.** There are seven cooperative rules, not twelve, each with an `[UNENFORCED U<n>]` marker at every site §19 names for it; the checker relates rows to marked sites in both directions, so moving a statement out of its named section fails. Rules the implementation should validate are no longer in this table — they became refusal codes in §4. |
| S4-27 | **Accepted.** §20 names Stage 6 and Stage 8 as behaviour-changing, so "one stage changes behaviour" is gone, and the non-downgrade rule is stated as the one thing that is *not* revertible, rather than alongside a claim that every stage is. |
| S4-28 | **Accepted in part; corrected 2026-09-11 after review 05.** The size bounds are real: §15.1 bounds result and rationale sizes and `log_cap` per stream per capture, and §21.3 does add journal I/O volume, evidence-read cost, a tiny-task overhead budget and a linear-graph case to the benchmark. The concurrent draining and the redaction were not specified in §11.2 or anywhere else, and claiming them here is how an unspecified rule looked answered. They are backlog rows owned by Phases 3 and 4. |

### The seven historical claims review 04 falsified

Each was re-read from the commit named, each was wrong, and each is corrected at its own row above
rather than here, so a reader of that row is not misled by it. Collected for the record:

| Claim | Where | What the commit shows |
|---|---|---|
| Revision 3 had the presence-derived label table and folded predicates | S01 | `fd24db1` has a mutable `state.json` lifecycle and transitions T1–T25; the defect belonged to an intermediate revision-4 draft |
| Revision 3's T21 used the status `revoked` | R3-05, and revision 4's model regression test | T21 said the status was no longer `authorized`, with hold reason `authorization_revoked`. The finding survives — `authorized` is not in the enum either — but the test asserted a string revision 3 never contained |
| Revision 4's §14 has no subsections | S02 | `812010c` has `### 14.1` |
| Revision 4's §14.1 prohibits further side effects and new required checks | S17 | §14.1 does not mention checks |
| Revision 4 rejects an uncovered required output at admission | S20 | §8.1 has no such condition; revision 5's §10.1 clause 8 does |
| Revision 3's citation table had twenty-one rows | S28 | It had twenty-six, so the drifted-row count was computed from the wrong total and is withdrawn |
| Revision 4's §19 has a reassessment stage | S29 | Its last row promises reporting and the benchmark only |

The pattern in six of the seven is the same: a disposition written while the revision it describes was
still being drafted, then never re-read against what was committed. The assessed/answered pair now
recorded in each disposition section, and checked by `test_parallel_execution_doc.py`, is the
structural half of the fix; re-reading the commit is the other half and cannot be automated.

## Review 05 — disposition

[`parallel-execution-review-05.md`](parallel-execution-review-05.md), assessing `44f1388`.
Assessed against revision 5; answered in revision 6. Nine findings — six P1, three P2 — plus a
sixteen-row backlog assigned to owning phases, and a recommendation to stop the global design rounds
and gate the work behind implementation phases instead.

**Every finding was checked against source before disposition, and all nine hold.** None was rejected
and none was reduced. Two of them (R5-01, R5-02) describe contradictions this document had already
half-recorded and had not resolved; the remaining seven were not visible from inside the previous
revision's own vocabulary. The review's recommendation is adopted as written: §20 replaces the former
implementation stages with six phase-gated deliverables, Phase 1 is specified to implementable detail
in [`parallel-execution-implementation-plan.md`](parallel-execution-implementation-plan.md), and
phases 2 to 6 are an adjustable roadmap rather than a design to be approved now.

| ID | Finding | Disposition in revision 6 |
|---|---|---|
| R5-01 | One immutable acceptance file cannot represent the attempt's canonical mutations | **Accepted.** An acceptance is no longer one file. §7.1 gives each mutation its own directory `mutations/<receipt-id>/` holding `intent.json`, numbered `fences/<NNNN>.json` and `ack.json`, and §7.4's `acceptance` record is the intent inside it. A retry of the same intent publishes another fence, not another intent (§7.4), so `expected_revision` moved out of the intent and onto the fence — which is what made the old single file contradictory: it carried a value that changes on retry inside a record that may never be rewritten. Successive mutations of one attempt are different receipts and therefore different directories. |
| R5-02 | Capture identity and assessment still contradict immutable publication | **Accepted.** `capture_id` is derived, not chosen: it is `cap-` plus the first 32 characters of the capture's own `body_digest` (§7.3), so two captures of the same bytes are the same record and publish-if-absent reports `identical` rather than a conflict. Captures are numbered per execution under `captures/<check-id>/<NNNN>-<capture-id>.json` and assessments under `assessments/<check-id>/<NNNN>.json`, with §11.4 capping a check at sixteen executions per attempt so the sequence is bounded. An assessment names the capture it assessed by `capture_id` *and* by `capture_body_digest`, so an assessment can never be read against a capture it did not see. |
| R5-03 | Stop and failure-budget semantics can still permit new work | **Accepted.** The stop gate is durable and project-level: §10.1 reads an unmatched `stop-request` under `control/stop-requests/`, and only a `stop-clearance` naming that request's sequence lifts it, so a restart does not clear a stop and neither does a takeover. `open_causes` is likewise project-level (§14.2), not per task: an unresolved failure closes the dispatch gate for the whole project, because the coordinator cannot establish that a cause is confined to the task that surfaced it. §14.3 computes the budget from ordered resolutions so it survives a restart. The reference model's `test_a_takeover_does_not_lose_a_durable_stop_request` is the executable case, and it is the case that found the fencing defect recorded below. |
| R5-04 | Scope identity and asynchronous event recording remain incomplete | **Accepted.** Scope ids are `worker` and `check:<check-id>#<NNNN>`, one per *execution* of a check rather than one per check (§6.1), carrying the same sequence the capture carries; §7.3 gives the filename encoding (`:` → `%3A`, `#` → `%23`, filename component only), which is what a scope id needs before it can be a path. Every scope is declared by a `scope` record published *before* the thing it covers runs, and sealed by a separate `sealed/<scope-id>.json`, so `open_scopes` is a difference of two record sets and never an inference from a process. |
| R5-05 | Ownership establishment and journal fencing are not yet implementable as stated | **Accepted, and the review was right about more than it claimed.** §8.1 gains O19 `acquire` and O20 `relinquish`, so first ownership and clean handback are operations with fences rather than an unstated precondition; §12.3's table states the fence and the candidate separately for all four cases, because O17, O19 and O20 exist precisely to change the two fields revision 5 said a candidate had to leave alone. §12.4 fences journal writes per attempt **and at the store root** — the second half was missing, and it is the defect recorded below. |
| R5-06 | Several data contracts cannot represent the advertised work | **Accepted.** §7.4's schemas are restated against what the operations actually publish, and the two envelopes are separated as the backlog asks: a common envelope for every record, an attempt envelope adding `task_id` and `attempt_id`, and the project-level kinds (`owner`, `stop-request`, `stop-clearance`, the store-root `generation-fence`) carrying the common one only. `config.json` and `plans/<task-id>.<plan-hash>.json` are stated to be not journal records and to carry no envelope at all. §7.5 classes every path by durability, and a third class is named for what a worker writes. |
| R5-07 | The new model improves structure but still tests a different protocol | **Accepted; the model was rewritten rather than patched.** All five probes the review reports now have named regression cases in `ReviewFiveRegressionTests`. The structural repairs are that `perform` is the only way an operation reaches the store, so recovery cannot apply effects no guard approved; a `MISSING` sentinel separates a stored null from an absent record; the launch capability is read off the registry field O4 sets rather than off a worker-written result; `open_causes` is project-level; and I3 is checked at the commit effect rather than at the intent. Building it is what found both defects recorded below. **It still does not establish that the protocol is correct**, and its docstring says which operations it does not model. |
| R5-08 | The first real execution target is unresolved | **Accepted.** Phase 2 of §20 is a one-host feasibility spike on a disposable project whose only question is whether a separate agent can be given a task, observed, and its scopes sealed — before any framework is built on the answer. §17.1 already records first-hand evidence from this host that a completed subagent is resumable, so "finished" is not "sealed" here; the phase exists to settle whether any admitted adapter behaves otherwise, or the envelope narrows again. |
| R5-09 | The measurement does not justify the stated capacity or rollout gate | **Accepted.** §3.2's figures keep their disclaimer and it now names what retires it: the harness and its graph fixtures are committed by Phase 6, and until then the figures are reproducible in principle and **not yet reproducible in this repository**. `max_concurrent` 2 is the pilot's setting rather than a measured capacity, and Phase 6's exit gate is real overlap shown safe, useful and measured against a sequential baseline. The documentation checker holds the disclaimer to a phase §20 actually carries. |

### The sixteen backlog rows, and where each landed

The review assigned these to owning phases rather than to this revision. Six of them turned out to be
statements the reference makes wrongly rather than work a phase will do, so they are fixed here; the
rest stay assigned, and Phase 1's specification carries the first row explicitly.

| Detail | Owning phase | Landed |
|---|---|---|
| Read/write mode alongside claim strings, namespace comparison, protected-path derivation | 1 API; 3 plan schema | **Fixed here for the API half.** Phase 1's `Claim` carries `access` (`read`/`write`) as a field, and the plan document states the comparison as component-wise ancestry over resolved keys with `store:` keys opaque. The plan-schema half stays with Phase 3. |
| Validate task/check/cause/scope/owner ids before using them in paths | 3 | Assigned. §7.3 gives the encoding; validating the ids is Phase 3's, with the schemas. |
| Separate common envelopes for project-level and attempt-level records | 3 | **Fixed here.** §7.4's two envelopes, and the four project-level kinds named as carrying the common one only. |
| Every identity-bearing array needs an actual order rule | 3 | Assigned. §7.3 states the canonical serialization; per-field order rules land with the schemas they belong to. |
| Log digests, bounded draining, redaction, and the settings the text references | 3 and 4 | Assigned, and **S4-28 is corrected below**: this document claimed those rules were already specified, and they are not. |
| Mutable grant capability updates need locked atomic replacement | 3 and 4 | **Fixed here in the statement.** §10.4 states the registry as a mutable table under its own lock, distinct from the immutable journal, and the reference model holds it as a separate structure for the same reason. The implementation is Phase 3's. |
| Define the classification enum | 3 and 4 | Assigned. §14 gives cause classes; the classification schema is Phase 3's. |
| Stop using labels in capacity, admission and closure | 4 | **Fixed here.** O20's precondition was the last one stated over a label; it now reads `grant_present` and `released`, and the documentation checker fails any §8.1 precondition naming a §6.2 label that is not also a canonical status. |
| Operation-aware canonical receipt lookup | 4 and 5 | Assigned. |
| Define when first ownership is cleared to null | 4 and 5 | **Fixed here.** O20 `relinquish` is the operation that does it, and §12.3 states its fence. |
| Do not say "never fsynced" while prescribing a fsyncing helper | 3 | **Fixed here.** §7.5 replaces the claim with durability classes: D1 is fsynced, D2 is not, and each path is named in one of them. |
| Distinguish a call returning from a process tree exiting | 2 and 4 | **Fixed here in the statement.** The `sealed` record carries `tree_exited` separately from `exit`, and §17.1 records the host evidence that the two differ. Phase 2 tests it. |
| Respect `checks[].executor` | 2 and 4 | Assigned. |
| Schema-version immutability at activation | 5 | Assigned to Phase 5, which owns activation. |
| Retention closure for approved resolutions and superseded captures | 5 or later | Assigned. |
| Fairness: replace asserted fairness with a deterministic tie-break | 6 | Assigned. |

### Two defects revision 6 found by building the model, that no review named

Both were found by writing the reference model of §21.2 against the reference and watching it fail, and
both are now checked by `test_parallel_execution_doc.py` as relationships rather than as phrases.

| Defect | What was wrong | Fix |
|---|---|---|
| `identical` compared bytes | §7.2 step 4 read the existing file and called the publication `identical` only on **equal bytes**, while §7.4's common envelope carries `coordinator_run`, `ownership_generation` and a second-precision `written_at` — all stamped by the writer, none derived from the record. A retry, and sharply a successor completing a predecessor's operation after a takeover, re-derives the same record and cannot reproduce the same bytes, so byte comparison reports `conflict` for exactly the publications §8.2's O3 and O7–O11 rows promise are `identical`. Revision 6 held both statements at once. | §7.3 defines `content_digest` as the same canonical serialization minus `body_digest`, `receipt_id` and the four writer-stamped fields — a comparison function, not a field. §7.2 step 4 and its first property use it, and §8.3 states content idempotence. The checker requires whatever `content_digest` removes beyond `body_digest`'s removals to be a common-envelope field and no other table's. |
| Only `attempts/` was fenced | §12.4 fenced journal writes per attempt, and an attempt's fence can only list paths under that attempt. Nothing could therefore ever explain `control/stop-requests/0001.json` written in an earlier generation, so §6.3 read it as `indeterminate` for the rest of the project's life — which breaks the one guarantee §10.1 asks of the stop gate, at the moment it matters most. | A store-root `generation-fences/<generation>.json` with `scope: "project"` covers `owners/`, `control/stop-requests/` and `control/clearances/`; O17 publishes it alongside the per-attempt fences. The checker walks §7.1's tree and requires every top-level family to be fenced, exempted by §7.5's D2 class or §7.4's non-journal list, or to be the fence itself. |

### Two dispositions of review 04 that were wrong, corrected

The review is right that this document is not reliable enough to be a completion checklist, and it
names two rows to prove it. Both are corrected in place above, and both were the same failure: a
disposition written about a draft and never re-read against what shipped.

- **S4-18** described escaping for external claim keys. External claims are refused — `local` and
  `store` are the only namespaces, and Phase 1's validation refuses `external` outright — so there is
  nothing to escape and the row promised a mechanism the design had already removed.
- **S4-28** claimed stream-draining and redaction rules in sections that do not carry them. They are
  not specified anywhere in the reference; they are backlog rows owned by Phases 3 and 4, and the row
  now says so.

Section numbers in dispositions written before revision 4 refer to the revision named in their own
table heading, which the preamble states. That is the most this document can offer: a superseded
number is not a defect, but a superseded *claim* is, and the two above were claims.

## Three defects the walkthrough found, that no review named

The revision brief asked for ten scenarios to be walked before declaring completion. Three of them did
not resolve against revision 3, and the walkthrough is recorded here because none of the three was a
review finding: a reader who only diffs this document against review 03 would not otherwise know why
these sections changed.

| # | Scenario | What did not resolve | Fix |
|---|---|---|---|
| W1 | Editing an existing file successfully | §8.2 derived `path:<resolved parent-or-self>` from a declared output. On the parent reading, a task writing `/repo/a.py` claims `/repo`, so no two tasks writing anywhere in one repository are ever concurrent and the protocol has no purpose. It also contradicted §8.2's own worked example, which treats `path:/repo/b.py` as a claim a task holds. The `.git` bullet had the same shape: read as "any task touching a file in a repository", it serializes every plan. | §8.2 now derives **the output path itself, never its parent**, says a directory output claims the directory and lets ancestry cover its contents, states that creating a file needs no claim on its parent, and scopes the `.git` claim to a task whose subject **is** the repository — one that commits, branches, rebases or fetches. |
| W2 | Inspection-only and multiple-command verification | §11.1 named an executor axis — worker or coordinator — and then nothing recorded it, no protocol step ran it, and §11.5's "a capture exists" could not distinguish a capture that is missing from one the worker was never asked to produce. Every `independence: separate_actor` check in every plan would therefore have reached `hold/adequacy_failed` (T14) on a perfectly good result. | `executed_by` is now a required field of each resolved check (§11.2), `separate_actor` and `human` force `coordinator`, the reverse is a derivation error, §7.2 gains the step, new **§11.6** says what the coordinator runs and what it refuses to forge, and §11.5 states that adequacy is evaluated after §11.6 and that the one absence it still reports is a missing *human* assessment. |
| W3 | Global verification versus a scoped writer | §11.6 runs a coordinator check under the attempt's existing grant, but a check's subjects contributed no claim, so a global check had no claim covering what it reads — and §8.3 forbids acquiring one late, by design. The two rules could not both hold. | §8.2 now derives `path:<subject>` with `access: read` from every resolved check, so a global check contributes `path:<repo-root>` read and correctly excludes a concurrent writer. Claim-set normalization is stated with it: duplicate keys merge, `write` subsumes `read`, and conflict is evaluated only between distinct grants, so an attempt is never in conflict with itself. |

The other seven scenarios resolved without a change: dispatch fencing (§12.2, §12.3), crash prefixes
(§6.3), a published failure with no pause flag (§13.1), partial-output reconciliation (§13.3), receipt
idempotence (§7.5), the registry's no-exemption rule for inline executors (§8.5), and the version-4
canonical gate (§5.5). W2 and W3 are also now covered by model assertions (§20.2); W1 is a derivation
rule the model does not implement, which is stated there as a limitation rather than papered over.

## Conditions for reconsidering a database and an MCP transport

Restored from revision 2 (S29). These are the conditions under which the Stage 8 reassessment (§19)
would adopt a database and an MCP transport, recorded so the reassessment is against written
conditions rather than remembered ones.

A database would need all of:

1. **Attempt-based primary keys.** A row identifies an attempt, never a task, so a retry cannot
   overwrite the record of what the previous attempt did.
2. **Integration state as an explicit column,** not inferred from the presence of other rows.
3. **Eligibility and reservation in one transaction.** Checking that a task may run and reserving its
   resources must not be two statements.
4. **Foreign keys enabled on every connection.** SQLite's default is off per connection, so this is a
   connection-setup obligation and not a schema property.
5. **No transaction held across execution or verification.** A transaction may not span a subprocess.
6. **A WAL-aware backup path.** Copying the main database file alone is not a backup in WAL mode.
7. **A patched SQLite.** The floor runtime here is `/usr/bin/python3` 3.9.6, whose bundled SQLite is
   3.51.0; review 01 reported a WAL reset corruption bug fixed in 3.51.3, citing
   `https://www.sqlite.org/wal.html#walresetbug`. That URL could not be fetched from this environment,
   so it is recorded as **the reviewer's finding, unverified here**, and the condition is that the
   version question be settled before adoption rather than that the bug be assumed.

An MCP transport would need both of:

8. **An explicit protocol-version target,** checked against the published stdio specification
   (review 01 named the 2026-07-28 revision), rather than a hand-rolled JSON-RPC dialect.
9. **MCP adapting operations only.** It may expose operations the coordinator already owns; it may
   never own scheduling, reservation or recovery, because then the transport becomes the protocol.

One correction to how this was recorded before: the repository's earlier rejection of SQLite was
narrower than "no SQLite" — it was about FTS5 plus a compiled `sqlite-vec` extension, not about the
stdlib `sqlite3` module.

## Rejected, deferred and revisited

| Option | Status | Reasoning |
|---|---|---|
| A per-project SQLite queue (`<project-dir>/queue.db`) | **Rejected** | Disproportionate to a mean of 1.86 parallel tasks, and its selecting argument — projected `.project.lock` contention — does not hold against the code (see above). Revisit at Phase 6 against the conditions above. |
| MCP server as the worker transport | **Deferred to Phase 6** | Adds a process and a protocol to reach a store the coordinator already owns. Reconsider if measurement shows the journal is the bottleneck. |
| Wave-synchronous scheduling as a fallback | **Rejected** | Idles a worker whose task finished early behind the slowest member of its level; §2's capacity comparison shows continuous dispatch captures nearly all of the available benefit. |
| A separate sequential code path | **Rejected** | It would be the least-exercised code on every degraded host. Sequential execution is this scheduler at capacity one (§4). |
| Capacity one as the response to a refused reservation | **Rejected in revision 4** | It permits precisely the conflicting write the reservation exists to prevent (R3-01). A refused grant refuses the task. |
| Workers claiming their own next task | **Rejected** | At four workers the coordinator already owns every decision before and after execution; self-claiming adds a distributed protocol to a problem that has none. |
| Timeout-based coordinator takeover alone | **Rejected** | It can hand ownership to a second coordinator while the first is still running. Replaced by run identity plus a canonical `ownership_generation` (§12.1). |
| `ownership_generation` in `runtime.json` | **Rejected in revision 4** | A fence a superseded coordinator can win a race against is not a fence. It is canonical, and every mutation carries it (R3-02, §12.2). |
| PID-based ownership | **Rejected** | The CLI process exits while a session-long coordinator keeps working, so a PID lock looks stale while the owner is live. A pid is also not stop evidence (§13.2). |
| An `exclusive` claim namespace | **Rejected in revision 4** | A namespace an ordinary path claim cannot see is not exclusion (R3-03). Two namespaces and a symmetric ancestry relation instead. |
| A durable `dispatch_paused` flag | **Rejected in revision 4** | A flag can be lost in the crash that made pausing necessary. `dispatch_blocked` is a predicate over records that were already written (§7.3). |
| `atomic_write_text` as the publication primitive | **Rejected in revision 4** | `os.replace` overwrites unconditionally, which is the wrong semantics for a record that must be immutable (R3-14). Publish-if-absent instead (§7.1). |
| Inferring `method: inspection` when a check string will not tokenize | **Rejected in revision 4** | It silently replaces a required executable check with a look at the file (R3-09). Rejection at admission instead. |
| Automatic retry after lost contact | **Deferred** | Requires a verified confinement capability no host currently asserts. A missed deadline is not evidence that a worker stopped. |
| Isolated staging directories | **Deferred** | A changed working directory is not confinement, and promotion needs its own protocol (§9). |
| `worktree` and `copy` modes | **Deferred** | No integration protocol; §21 names what each would need (R3-15). |
| Lock upgrade at verification time | **Rejected** | Deadlocks with two tasks. Upfront total acquisition (§8.3) plus quarantine-and-re-prepare instead. |
| Per-task `started_at` / `ended_at` fields | **Deferred** | `TASK_FIELDS` is both the required and the allowed set, so a field cannot be optional until they are split, and an old installation rejects unknown fields anyway. Timing lives in captures and attempt records instead (§15). |
| A sidecar file in `execution/` as the old-reader marker | **Rejected** | An old installation never looks there. The fail-closed marker is a `project.json` `schema_version` bump, which an old reader refuses outright (§5.5). |
| Changing `schema_version` in an ordinary commit | **Rejected in revision 4** | `schema_version` is in `IMMUTABLE_PROJECT_FIELDS` [C21], so it is not possible. A dedicated `enable-execution` operation instead (§5.5, R3-12). |
| Commit batching as a concurrency optimization | **Deferred, and no policy is claimed** | The skill already permits one commit to advance several tasks; this design states no batching policy. |
| Literal JSON Schema documents in the reference | **Rejected** | Implementation inside a design document, and inert without a validator. Field-level tables plus a compatibility rule instead (§5.2, §5.5). |
| Post-task comparison of touched paths as enforcement | **Rejected as enforcement, kept as diagnostics** | With several concurrent attempts it cannot attribute a change to one of them, and it cannot see a write that was later reverted (U2). |
| A machine-scoped reservation registry | **Deferred** | Would close cross-workspace conflicts, but a second workspace root on one machine is a defect to report, and workspace discovery is a new mechanism. §8.5 scopes the guarantee and §21 records this (R3-01, S15). |
| Hardlink aliasing detection | **Deferred** | Two claims may name one inode through different paths; §6.6 states the limitation rather than implying coverage (S11). |

## What is still unmeasured

Stated here so it is not mistaken for settled:

- **Context economy.** §1 names it as plausibly the larger benefit, as a hypothesis, and only for
  hosts that assert bounded worker context. Coordinator context consumption has never been measured
  against a sequential baseline.
- **Contention.** Whether locking under concurrency is material is unknown. Revision 1's projection
  that it would be is falsified as an argument, which is not the same as a measurement that it is not.
- **Executor overhead.** Never measured, which is why the reference claims only the structural limit
  its sample demonstrates and makes no comparison between plan shape and executor cost.
- **§2's figures from this repository.** Reproducible in principle; the script and fixtures are a
  Phase 6 commitment and are not in this branch.
- **Protocol correctness.** The reference model of §21.2 exercises the decision functions as *specified*,
  so they cannot find a defect the document and the model share. Nothing here has been run against a
  real filesystem, real concurrency, or a real host.
