# Parallel execution design — review 02

Date: 2026-09-11

Reviewed branch: `parallel-execution-design`

Reviewed commit: `3eabe498a806b30a6500a4094ffbcbb1bb2b2b11`

Design: [revision 2 at the reviewed commit][design]

Previous review: [parallel-execution-review.md](parallel-execution-review.md)

Line numbers and section references below refer to the reviewed design snapshot, not subsequent
revisions. This review covers the full revised design, the previous review, and the relevant existing
implementation. The parallel executor remains unimplemented; findings concern the proposed protocol.

## Assessment

Revision 2 is substantially better, but changes are still needed before implementation. It addresses
the earlier architectural objections; it does not yet specify enough of the execution protocol to
establish the recovery guarantees it claims.

Several findings concern gaps in the earlier review's proposal too. Those suggestions were an
architectural outline and need more precise rules before becoming an implementation specification.

Priority meanings:

- **P1:** Potential correctness or recovery failure.
- **P2:** Significant implementation ambiguity or compatibility problem.

## Principal findings

### 1. P1 — A crash during spawning remains indistinguishable from “never launched”

Design reference: §11, line 461.

The recovery table permits redispatch when the canonical task is `RUNNING` but its worker never
launched. It does not cover this sequence:

```text
Commit task RUNNING
    → host starts worker
    → coordinator crashes before recording the returned host handle
    → restart sees no handle
    → launches a replacement
```

Both workers can now execute. The revised lease rule does not prevent this because recovery may
incorrectly classify the first worker as never launched.

Add a durable `DISPATCHING` state before calling the host. Require an idempotent launch key or a way
to discover workers by attempt ID. If the host provides neither, interrupted dispatch becomes
`UNCERTAIN`; absence of a recorded handle must never authorize redispatch.

Also distinguish worker termination from termination of everything it launched. A host reporting an
agent as finished does not by itself establish that background commands stopped.

### 2. P1 — The canonical acceptance record is still unspecified

Design reference: §5.2, line 196; §10.5; §11.

An integration receipt contains “the canonical revision that accepted” it. But the receipt is
immutable, and acceptance has not happened when a receipt is written before the canonical commit.

Consider preparing a receipt for revision 12, appending its evidence, then losing the revision race.
Retrying at revision 13 changes the receipt content and therefore its hash. That undermines evidence
deduplication based on receipt identity.

More fundamentally, the document never specifies exactly how `project.json` identifies the accepted
receipt. Finding a receipt on disk is insufficient: it could belong to a commit that failed.

Specify these separately:

- An immutable result/verification receipt whose identity does not depend on a future revision.
- A canonical task evidence reference to that exact receipt.
- An acknowledgement recording the revision actually observed after successful commit.

Recovery should recognize acceptance from the canonical reference, not from a receipt's self-reported
revision. The existing evidence validator checks file existence, not receipt semantics or content
hashes; that additional validation needs an explicit implementation task. See
[`_validate_evidence_reference` at the reviewed commit][evidence-validator].

### 3. P1 — Atomic replacement does not provide the required immutable, multi-file publication protocol

Design reference: §5.1, line 185.

The existing atomic-write helper is useful and already flushes file contents and synchronizes the
containing directory. The missing guarantee is ordering across files.

For example:

```text
Publish result.json
    → crash before publishing verification.json or its logs
```

The coordinator sees a complete JSON object referencing incomplete evidence. Conversely, publishing
a different `result.json` through atomic replacement can overwrite the first result. Atomic
replacement explicitly permits replacement; it does not enforce immutability.

Define a publication protocol:

1. Write logs and verification artifacts first.
2. Finalize and hash those artifacts.
3. Publish the result manifest last.
4. Treat an identical repeated publication as success.
5. Reject a different publication for the same attempt.
6. Distinguish incomplete publication from a valid failed result.

Also classify `runtime.json`, live dispatch records, and reservations in §5.3. Their recoverability
is currently omitted. “A dispatch record for an attempt that has not executed” is disposable only
when non-execution is established, not merely when a start timestamp is missing.

### 4. P1 — Holding reservations through integration needs an explicit rule against lock-upgrade deadlocks

Design references: §7.2, line 326; §7.4, line 343.

The following interpretation is currently possible:

```text
A holds write reservation on a.py
B holds write reservation on b.py

A finishes and needs exclusive repository access for verification.
B finishes and needs exclusive repository access for verification.

Neither releases its reservation until integration.
Neither can verify while the other reservation remains.
```

The design needs to choose between:

- Reserving the entire execution-and-verification resource set upfront. This avoids upgrades but
  serializes tasks requiring repository-wide checks.
- Explicitly verifying a closed group of completed tasks together, with all its writers stopped.
- Verifying isolated snapshots, followed by separately scheduled integration.

“Coordinator actions participate in resource conflicts” is insufficient without acquisition-order
and upgrade rules. Add this exact two-task scenario to the tests.

### 5. P1 — Coordinator ownership is named, but takeover is not designed

Design reference: §11.2, line 492.

The guard needs defined acquisition, ownership, expiry, takeover, and release operations.

A short-lived CLI process cannot own a session-long coordinator lock merely through its PID: the
CLI exits while the coordinator remains active. Conversely, timeout-based takeover can leave the
old coordinator running.

Specify a durable run identity and ownership generation. Every coordinator mutation must verify
that generation under the relevant lock. A superseded coordinator must be unable to publish
dispatches or integrate results through the supported protocol.

Separate coordinator ownership generation from attempt identity. Otherwise, takeover may invalidate
results from workers that recovery is supposed to adopt.

The document also needs the lock order between the execution store, canonical commits, and target
reservations. Without it, implementations can introduce deadlocks while attempting to enforce the
ownership rule.

### 6. P1 — The contract hash omits execution-relevant state and has no derivation contract

Design reference: §6.3, line 259.

The listed hash inputs omit task name, dependencies, effect classification, authorization scope,
project working directory, and execution mode. Several affect what the worker is allowed or
expected to do.

“The task's own definition changed” does not cover a changed project-level `working_directory`.
Hashing textual output references also does not necessarily detect that their effective destinations
changed.

Define a versioned contract object containing the execution-relevant values, including resolved
roots and verification method/arguments. Specify serialization, ordering, hash algorithm, and
treatment of missing values.

Authorization revocation also needs a separate rule. A matching frozen hash must not authorize an
action after its authorization has been withdrawn.

There is an earlier dependency here: canonical tasks do not currently contain structured resources,
input identities, or verification methods. State how the coordinator derives those fields, records
that derivation, and determines whether a later change invalidates an attempt.

### 7. P1 — Staging directories do not establish the confinement needed for automatic retry

Design reference: §11.1, line 486.

The exception correctly requires the old attempt's writes to be confined to disposable storage. The
next sentence says this is what staging mode provides.

A staging directory alone provides no such confinement. A worker can still write an absolute target
path or leave a subprocess running outside it—the same limitation correctly acknowledged for
worktrees elsewhere.

Change the exception to require an explicitly verified confinement capability. Merely changing the
working directory does not qualify.

Promotion also needs its own protocol: verify the target baseline still matches, identify already
promoted outputs after interruption, and define recovery after partial multi-file promotion. Until
those rules exist, staging should remain an explicitly deferred mode.

### 8. P2 — The state machine has paths that cannot release reservations

Design reference: §6.1, line 228; §7.4; §11.

Reservations release only at `INTEGRATED`, but the lifecycle does not define terminal paths for:

- A discarded `PREPARED` attempt.
- A definitively failed launch.
- An invalidated contract.
- A quarantined conflicting result.
- A user cancellation.
- An uncertain attempt subsequently proven never to have executed.

The prose says failed results are integrated, but the diagram routes all integration through
`VERIFIED`, whose meaning for failure is unspecified.

Provide a transition table with triggering event, preconditions, durable writes, canonical
transition, and reservation disposition. Add an explicit abandoned/reconciled terminal state where
appropriate.

Also define handling of a valid late result from an `UNCERTAIN` attempt. The identity rule currently
talks about a “live attempt,” which could cause recovery to reject the exact result it needs.

### 9. P2 — Failure handling promises bounded shutdown without specifying a possible terminal action

Design reference: §12, line 500.

The design simultaneously requires:

- No cancellation of in-flight attempts.
- Reservations retained until execution is reconciled.
- Bounded shutdown.
- The project remaining `EXECUTING` while tasks are `RUNNING`.

These can support a bounded observation period, but cannot guarantee bounded termination when a
worker's status remains unknown.

Say what happens at the bound: stop waiting, persist an unresolved state, retain reservations, and
report the unresolved execution. Do not imply that shutdown or reconciliation necessarily
completes.

The pause rule also begins only when a worker reports `failed` or `blocked`. It must cover
coordinator verification failure, malformed results, receipt mismatch, capture failure, and contract
invalidation. Recovery should scan pending failures before admitting work even when `runtime.json`
still says dispatch is enabled.

### 10. P2 — Verification provenance is confused with verification validity

Design reference: §10.2, line 423; §10.3.

A process capture proves which command ran and what it returned. It does not establish that the
command was the required verification or checked the submitted artifact.

Acceptance must compare the captured command, working directory, attempt, and artifact identities
against the execution contract. A receipt for `true`, or a test run followed by an artifact
modification, must not satisfy an unrelated required check.

The categorical “Strongest / Strong / Weakest” ranking also overstates what provenance alone
establishes. A coordinator can execute an inadequate command; an independent human review can
provide stronger substantive evidence.

There is a concrete modeling error in §10.3: a coordinator's own inspection is forced into
`worker-reported`, even though no worker reported it. Separate:

```text
method: command | inspection | review
actor: coordinator | worker | human
capture: process-record | structured-assessment
```

Record independent review separately from self-review where the task requires it.

## Additional implementation issues

| Priority | Design location | Issue and recommended correction |
|---|---|---|
| P2 | §7.2, line 318 | `external` references include URLs and durable identifiers. They cannot become absolute real paths. Use separate filesystem and external-resource namespaces. |
| P2 | §7.2, line 316 | Define read/write modes and their conflict matrix. Directory ancestry alone should not make two readers conflict. Specify how missing paths, aliases, and case sensitivity are handled. |
| P2 | §8, line 366 | “Declining” cross-project concurrency requires a way to detect it. Either provide target-level admission or state this is only an operator precondition. Identical and nested target roots both matter. |
| P2 | §15, line 574 | Capacity one does not restore missing result delivery, persistence, or recovery. Distinguish no worker support—use an inline executor—from missing correctness prerequisites—do not start this execution protocol. |
| P2 | §6.2, line 244 | Heartbeats still exist, but their transport, persistence, renewal rules, and deadline authority disappeared with MCP. Define them or remove heartbeats from the first version and use host observations. |
| P2 | §13, line 525 | Sidecar receipts avoid new task fields, but do not solve old-reader compatibility. An old installed host can accept the canonical JSON while ignoring active attempts. Define a version gate or fail-closed marker for projects using this protocol. |
| P2 | Stage 4, line 711 | Reusing capture code requires changes: current `record_evidence` raises on timeout or launch error before appending evidence, and buffers captured output in memory. Define durable capture failures, bounded output handling, decoding, and subprocess cleanup. |
| P2 | §5.1, line 169 | One `verification.json` and two log files are ambiguous for multiple verification commands. Use individually identified captures referenced by a manifest, preserving failed checks and retries. |
| P2 | §5.3, line 206 | Durability classification is not a retention policy. Define which records can be collected, whether logs must survive closure, how referenced receipts are protected, and what happens on missing/corrupt records. |

For the capture-helper point, Python documents timeout handling for the direct child process; that
is not a general guarantee of terminating an arbitrary descendant process tree. The host/capture
contract must make that distinction. See the [Python subprocess documentation][python-subprocess]
and the [existing capture implementation][capture-code].

## Smaller corrections

1. **The review contained six high-priority findings, not seven.** The revision introduction and
   commit description miscount them.

2. **“Elapsed durations … reported in UTC” is incorrect.** Durations are reported in seconds or
   another unit; timestamps use UTC. Design line 200.

3. **§5.1's writer ownership sentence contradicts output editing.** It grants the worker its result,
   verification, and logs “and nothing else anywhere.” Scope that sentence to the execution store.
   Design line 181.

4. **“Read-only workers … Nothing to isolate” is too broad.** Readers can observe changing inputs,
   and result files still have ownership requirements. Design line 358.

5. **The protected-file lists disagree.** §7.2 includes `briefing.md`; §9 omits it. Neither clearly
   protects coordinator-owned execution records, lock files, or other shared records from resource
   grants. Define one rooted protection policy and reference it.

6. **“Priority (dependency depth, then id)” leaves the depth direction unspecified.** Ascending
   level and descending remaining critical-path depth are different policies. State the exact sort
   key. Design line 309.

7. **“The critical section is short” remains unmeasured.** Removing index rebuilding from it does
   not establish its duration; candidate validation and filesystem work remain inside. Design
   line 115.

8. **“The bottleneck is the plan, not the executor” still exceeds the evidence.** The revised
   document expressly says actual executor overhead was not measured. Narrow this to the structural
   limitation demonstrated by the sample. Design line 70.

9. **Stage 6 is called “last,” but Stage 7 follows it.** Say “the last activation prerequisite.”
   More substantively, benchmark instrumentation should exist before activation if efficiency is
   an objective.

10. **The batching disposition names irrelevant sections.** §20 says §12 and §17.2 address commit
    batching, but neither specifies a batching policy. State the policy or say it remains deferred.
    Design line 780.

11. **The citation checker is not in the committed tree.** All 27 literal ranges were independently
    checked and pass. That does not make the document's claimed checker reproducible. Commit it or
    identify its external location and label the check accordingly.

12. **The retired-phrase checker is underspecified.** Historical discussion intentionally includes
    withdrawn claims. A whole-document phrase prohibition needs exclusions or it will reject the
    document's own explanation.

13. **The benchmark still does not ship with the figures in this branch.** Future-tense commitments
    are appropriate; present-tense statements that it is committed are not.

14. **The document is increasingly a response to the previous review.** Move most revision history
    and argumentative disposition text into `docs/`. Keep the implementation reference centered on
    current rules, operations, and failure behavior.

## Requested artifacts for the next revision

Concentrate on four concrete artifacts rather than adding more explanatory prose:

1. A complete attempt transition table, including launch ambiguity, abandonment, late results, and
   cancellation.
2. Versioned JSON schemas and a precise result-publication/acceptance sequence.
3. A resource acquisition policy that explicitly resolves verification upgrades and cross-project
   targets.
4. Host-adapter operation contracts, including unsupported capabilities and takeover behavior.

## Review verification and limits

- All **27 citation ranges** contained their expected literal text.
- All numbered top-level section references resolved.
- Repository searches did not locate the claimed citation checker or benchmark in the committed
  tree.
- The branch changes only documentation, so the unchanged Python regression suite was not repeated
  for this review.
- No executor implementation exists in this revision; concurrency failures above are protocol
  traces and specification findings, not failures reproduced against a running executor.
- The Python subprocess reference was checked on the review date.

The review itself made no repository changes. This file was subsequently added at the user's request
to preserve the second review next to the first.

[design]: https://github.com/nampham2/agents/blob/3eabe498a806b30a6500a4094ffbcbb1bb2b2b11/plugins/research/skills/project/references/parallel-execution.md
[evidence-validator]: https://github.com/nampham2/agents/blob/3eabe498a806b30a6500a4094ffbcbb1bb2b2b11/plugins/research/skills/project/scripts/workspace_lib.py#L457-L491
[capture-code]: https://github.com/nampham2/agents/blob/3eabe498a806b30a6500a4094ffbcbb1bb2b2b11/plugins/research/skills/project/scripts/workspace_lib.py#L2542-L2556
[python-subprocess]: https://docs.python.org/3.9/library/subprocess.html#subprocess.run
