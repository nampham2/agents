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
heading.** Revision 4 renumbered and merged several sections; §22 of the current reference is the
index of what it actually cites today.

## Revision history

Reconstructed from the commits on this branch and the checked-in review files, not from recollection.
Each row names the commit that is the revision.

| Revision | Commit | Date | What it actually contained, and what caused it |
|---|---|---|---|
| 1 | `e18e589` | 2026-09-11 | First design. §2 already carried the graph measurement — mean average level width 1.86, maximum 3.87, modelled speedup 1.45x mean — and made no 2-4x claim. §3 already cited `DirectoryLock` and `commit --expected-revision` as existing facilities. The architecture was **one SQLite database per project at `<project-dir>/queue.db`** in WAL mode (§5), a hand-rolled JSON-RPC 2.0 MCP server over stdio for worker access (§6), and a **ready queue as the primary scheduler with wave-synchronous scheduling as a fallback** (§7.4). |
| 2 | `3eabe49` | 2026-09-11 | Rewritten after [review 01](parallel-execution-review.md). Replaced the database and the MCP server with a file store inside the project directory; removed the wave-synchronous fallback in favour of continuous dispatch; withdrew the lock-contention argument; added the recovery contract, the resource model, the unenforced-guarantees table and a citation table; recorded nine conditions for reconsidering a database and an MCP transport (restored below). Its introduction miscounted review 01's findings as seven high-priority. |
| 3 | `fd24db1` | 2026-09-11 | Rewritten after [review 02](parallel-execution-review-02.md). Added the attempt transition table, `DISPATCHING`, the publication protocol, the receipt/reference/acknowledgement separation, record schemas with a version gate, the acquisition policy, coordinator run identity and lock order, the versioned contract object, the method/actor/capture model, host-adapter contracts and a retention policy. Deferred staging. Split this document out of the reference and committed the citation checker as a repository test. |
| 4 | *this revision* | 2026-09-11 | Rewritten after [review 03](parallel-execution-review-03.md). The store became a **journal** whose labels and gates are pure functions of the set of records present, published with a publish-if-absent primitive; reservations moved to a workspace-scoped registry every executor including inline must use; identity split into three sets so an intended edit invalidates nothing; the authorization predicates were rewritten against the real canonical enums; takeover became a revision-checked canonical commit; checks are resolved at planning time and shell composition is rejected rather than downgraded; captures became tagged records with per-check adequacy; `copy` and `worktree` modes were deferred. This document's fabricated history was deleted. |

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

[`parallel-execution-review.md`](parallel-execution-review.md). **Nine findings: six high-priority
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

[`parallel-execution-review-02.md`](parallel-execution-review-02.md). Ten principal findings — seven
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

[`parallel-execution-review-03.md`](parallel-execution-review-03.md), assessing `fd24db1`. Seventeen
principal findings — ten P1, seven P2 — and thirty-one smaller consistency issues.

**Every finding was evaluated against source before disposition, and all seventeen hold.** Three
recommendations are modified rather than adopted as written, and each modification is argued below the
table with what was checked. Nothing is dismissed; nothing is silently omitted.

| ID | Finding | Disposition in revision 4 |
|---|---|---|
| R3-01 | A failed target claim still permits a competing writer | **Accepted.** Revision 3 fell back to "capacity one" when the target claim could not be acquired, which is exactly the conflicting inline execution the claim existed to prevent. §8.5 puts one registry at `<workspace-root>/.execution-registry/`, states that **every executor acquires from it with no exemption for inline**, and makes failure to acquire a refusal to start *that task* rather than a mode change. §17's degraded-host fallback is now explicitly about missing worker capability only, never about a refused grant. |
| R3-02 | Ownership checking and canonical mutation have a fencing race | **Accepted.** `ownership_generation` is canonical rather than a `runtime.json` field (§5.3, §5.5), takeover is itself a revision-checked commit that raises it (§12.1), and every subsequent canonical mutation carries the generation it read *inside the same candidate*, so a superseded coordinator loses on the revision check instead of winning a check-then-write race (§12.2). Physical dispatch is fenced separately: the generation is re-read between the `start-permit` and the host call, and a raised generation aborts the launch (§12.3). |
| R3-03 | Coarse repository claims do not exclude ordinary path writers | **Accepted.** The `exclusive` namespace — the thing an ordinary `path` claim could not see — is deleted. §8.2 has two namespaces, `path` and `external`, and one **symmetric** ancestry predicate, so `path:/repo` write conflicts with `path:/repo/b.py` write in both directions. Verification no longer conflicts with itself: a coordinator re-run executes under its own attempt's existing grant and requests no new claim (§8.2, §11.4). |
| R3-04 | Recomputing the contract can reject a task's intended edit | **Accepted.** §6.6 splits identity into three sets: `definition` (what the task is), `baseline` (the frozen inputs and the pre-state of `writable_subjects`), and `produced` (what the writer wrote). `definition_hash` covers only the first. A declared output is a `writable_subject`, so its bytes changing is the expected outcome and invalidates nothing; `hold/contract_invalidated` (T25) fires only on a `definition` change before the permit was written. |
| R3-05 | T21 uses a nonexistent authorization status and omits the no-authorization case | **Accepted; the nonexistent status is a real defect.** `AUTHORIZATION_STATUSES` is `{not_required, pending, explicit, denied, deferred}` [C17] and revision 3's `revoked` is not in it. §8.1 conditions 3 and 4 are two named predicates — `delegable` and `in_force` — over the real enum, with `pending`, `denied` and `deferred` all not in force and reported distinctly. §14.1 makes withdrawal **one** canonical commit setting `RUNNING → BLOCKED` and `authorization.status: denied` together, both legal in one candidate. |
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
| S01 | **Accepted.** §6.1 is a 14-row first-match table yielding 14 labels, one row each, and the count is stated in its prose; claim-holding and finalization are stated as two separate predicates rather than folded into the row conditions, which is what revision 3 got wrong (§6.1). Quarantine is terminal for *scheduling* and not for claim-holding, which is the distinction that made "is quarantine terminal?" unanswerable; §5.2 states it. The checker derives its label set from the table, checks the table is a bijection, and compares it against both the prose count and the labels §6.2 uses, instead of hardcoding a count. |
| S02 | **Accepted.** The dangling `§14.4` is gone. §14 has no subsections in revision 4, the review-02 disposition above says "step 4 of §14", and the checker resolves every `§` reference to a heading, reporting zero unresolved. |
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
| S17 | **Accepted.** The hold rows are written over label ranges rather than hand-listed subsets: T26 and T27 span `PREPARED`…`RUNNING`, and T28 (`superseded`) and T29 span **any unresolved** label. A valid result from cancelled work is still recorded, and §14.1 states that a stop prohibits further side effects and new required checks. |
| S18 | **Accepted.** §14.1 separates three things that revision 3 collapsed: an execution failure, an operator stop request (`hold/stop_requested`, explicitly not a failure), and withdrawn authority (`hold/authorization_withdrawn`, one canonical commit with `denied`). Each makes a best effort to stop the worker and, where the host cannot, retains uncertainty and reports what could not be stopped rather than asserting it stopped. |
| S19 | **Accepted with the claim narrowed to what it proves.** §11.3 records `subject_digests_before` and `subject_digests_after`, and states in the same paragraph that equality shows the subject did not change *across* the check and **does not** show it was unchanged *during* the check — a check that writes a file and restores it looks identical. The stronger claim revision 3 made is withdrawn. |
| S20 | **Accepted.** A resolved check carries `subjects` and `independence` (§11.2), and §11.5 compares both per `check_id`. A required output with no check covering it is rejected at admission, not discovered at acceptance. |
| S21 | **Accepted.** §17's `spawn` returns `started(handle)`, `definitively_not_started`, or `ambiguous`; `discover` returns `running`, `never_started`, or `unknown` as three distinct results with distinct consequences; `launch_key` is an input to `spawn` and is recorded in `start-permit`. |
| S22 | **Accepted, by declaring the inline adapter's actual guarantees instead of promising equivalence.** §17 states that inline `spawn` runs synchronously and therefore never returns `ambiguous` — so inline has no dispatch-ambiguity window at all — that `discover` always returns `finished`, that `interrupt` is unavailable, and that `bounded_context` is false. No claim is made about the observability of subprocess descendants. |
| S23 | **Accepted.** See the second modified recommendation above: §13.2's five stop-evidence kinds are a closed set, and the absence of a visible write is not one of them. |
| S24 | **Accepted, and the unsupported claim is withdrawn.** The reference no longer says existing validation already catches a removed capture. Transitive evidence-reference validation is explicit Stage 4 work (§19), and §5.7's retention rule no longer leans on it. |
| S25 | **Accepted.** §5.7 requires a durable `collection` tombstone naming the files *before* they are removed: a missing file named by a tombstone is collected, a missing file with no tombstone is an error that stops dispatch until an operator reconciles it, and a crash between tombstone and deletion is re-run idempotently (§6.3). Removing an empty lock directory in normal operation is distinguished from collection under `execution/`. |
| S26 | **Accepted.** `max_log_bytes` is a required contract field (§6.5) and the capture record carries `truncated` per stream. §5.7 applies the skill's existing redaction rule before the bytes are written, so the digest covers the redacted content and there is no second unredacted artifact to protect. |
| S27 | **Accepted.** The reference is self-contained: its preamble states that `docs/` is not in the installed plugin subtree, no normative rule depends on this document, and there is no cross-subtree link a shipped plugin would follow. |
| S28 | **Accepted.** §22 was rebuilt from source rather than repaired: every needle was re-read at its stated line range, the two misattributed citations were replaced with the code they actually support, and the ids were renumbered contiguously with every inline reference remapped. Eleven of revision 3's twenty-one rows had drifted; the rebuild is why the checker's citation test is worth running. |
| S29 | **Accepted.** The nine conditions from revision 2 are restored verbatim in substance below, under "Conditions for reconsidering a database and an MCP transport", and §19's reassessment stage refers to them. |
| S30 | **Accepted.** §1 qualifies the context claim by host capability: bounded worker context is a host capability (`bounded_context`), it is false for the inline adapter, and the claim is stated for hosts that assert it rather than unconditionally. |
| S31 | **Accepted.** §8.4 defines the ceiling as **dispatched workers**: inline execution does not consume a slot but does hold claims, and non-terminal attempts and held grants are counted by the resource rule instead. §16 describes graph width separately from worker count, and no five-wide claim survives against a ceiling of four. |

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
| A per-project SQLite queue (`<project-dir>/queue.db`) | **Rejected** | Disproportionate to a mean of 1.86 parallel tasks, and its selecting argument — projected `.project.lock` contention — does not hold against the code (see above). Revisit at Stage 8 against the conditions above. |
| MCP server as the worker transport | **Deferred to Stage 8** | Adds a process and a protocol to reach a store the coordinator already owns. Reconsider if measurement shows the journal is the bottleneck. |
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
  Stage 2 commitment and are not in this branch.
- **Protocol correctness.** The model tests of §20.2 exercise the decision functions as *specified*,
  so they cannot find a defect the document and the model share. Nothing here has been run against a
  real filesystem, real concurrency, or a real host.
