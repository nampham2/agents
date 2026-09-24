# Project lifecycle automation inventory and implementation

Collected on 2026-09-24 against `de7d057` (PR #35, version 0.17.0).
Implemented in PR #35 following approval to implement the full inventory. The catalog below
preserves the original opportunities; the availability map is authoritative for shipped interfaces.

## Implemented command map

Use `research-project workflow <project> <action> -` with JSON on stdin, through either host's
resolved launcher. Agent-facing contracts and examples start in
[automation.md](../plugins/research/skills/project/references/automation.md).

| Inventory | Available surface |
| --- | --- |
| A01 | `init --json` |
| A02, A03, A04 | `workflow resume`, `read` with source selections, conditional hashes, sections and exact entries |
| A05, A06 | `workflow checkpoint`, `freshness` |
| A07, A08 | `workflow round`, `confirm` |
| A09 | `update --dry-run`, `workflow preview` |
| A10, A11, A12 | `workflow correct`, `impact`, `reconcile` |
| A13, A14 | `workflow packet`, `worker-event`; unresolved observations in resume |
| A15, A16, A18 | `workflow verify`, `fingerprint`; durable attempt outcomes and evidence-only retry |
| A17 | `workflow authorize`, `receipt` |
| A19, A20, A21 | `workflow review`, `readiness`, `finalize` |
| A22 | `workflow report`: requested Markdown scaffolding, accounting, generated citations and optional graph |
| A23 | `workflow maintenance`, `cancel` |
| A24, A25 | `workflow memory-health`, `assess`, `triage` |
| A26 | Metadata operation journals, same-ID replay protection and `workflow recover`; checks/triage have their own same-ID recovery |

Boundaries: fingerprints cover selected files/configuration, not remote state or inferred environment
dependencies. Report link checks cover generated rooted citations, not arbitrary prose links or claim
support; HTML still uses the existing report design/checker. Workers are observed through the host,
never scheduled or assumed stopped by local records. Consent, scope, acceptance and memory quality
remain supplied decisions.

### Verification and measured economy

`test_lifecycle_commands.py` exercises every capability, v3/v4 through both host launchers, immutable
history, conflicts, malformed input, launch errors/timeouts, lost acknowledgements, uncertain command
outcomes, partial document saves, post-commit repair and deduplicated memory promotion.

The bounded-resume fixture replaces five reads with one, retaining the selected task, specification,
design, continuation and actual evidence. Measured output was 8,301 → 7,848 UTF-8 bytes (about 5.5%
less), both with zero and 200 historical decisions. Paths affect absolute byte counts. This measures
calls and serialized output, not billed/model tokens or total project latency.

The core skill plus routine commands remains within the existing 1,500-word budget. The automation
router is 194 words; context, records, and execution/memory schemas are separate on-demand references.
An independent isolated trial completed alignment through DONE and resumed the saved result; closure
validation passed through both launchers. That tests launcher behavior, not actual host-app restarts.

The next useful automation is to assemble context, maintain related records, and recover partial
operations in code. Agents should supply decisions and interpretation once; scripts can derive
identifiers, hashes, links, timestamps, state patches, and consistent document rendering.

This inventory covers 26 additional opportunities. They are capabilities to combine into a small
number of operations, not a proposal for 26 new commands. Priority reflects expected recurring
bookkeeping savings; it is not a measured token or latency ranking.

## Baseline automation before this implementation

Reuse these rather than rebuilding them:

| Capability | Existing implementation |
| --- | --- |
| Workspace discovery, initialization and project lookup | `find-roots`, `init`, `list-projects`; `allocate_project` and `list_projects`. |
| Bounded context, document hashes and validation | `context --validate`; `validated_project_context`. |
| Task/dependency projections | `context --task --task-only/--worker`; `_worker_context`. |
| Partial state patches and defaults | `update`; `update_project_data`, `_new_task`, `commit_state`. |
| Guarded document edits and retry-safe prose appends | `edit`, `append --entry-id`; `edit_document`, `append_record`. |
| Task transitions and finish/start-next composition | `task`; `task_operation`, including `REVIEW → EXECUTING` on start. |
| Real command evidence and exact evidence selection | `record-evidence`, `read evidence --entry`; `record_evidence_result`, `selected_evidence_references`. |
| Reflection, closure, index rebuilding and closure validation | `close`; `close_project`, `_rebuild_index_after_commit`. |
| Dependency visualization | `show-graph`; `build_task_graph`. |
| Memory retrieval, promotion, compaction and retirement | Existing memory commands in `manage_workspace.py`. |
| Report structure/HTML checks and migration previews | `research-validate` report options; `migrate`. |

Source map:
[CLI](../plugins/research/skills/project/scripts/manage_workspace.py),
[session/context](../plugins/research/skills/project/scripts/workspace_session.py),
[documents](../plugins/research/skills/project/scripts/workspace_documents.py),
[operations](../plugins/research/skills/project/scripts/workspace_operations.py),
[evidence](../plugins/research/skills/project/scripts/workspace_evidence.py),
[core primitives](../plugins/research/skills/project/scripts/workspace_lib.py).
The [earlier automation plan](project-workflow-automation-plan.md) describes the implemented first
generation and contains historical interface descriptions; current source takes precedence.

## Opportunity catalog

Priority **A**: implement first for frequent work or as a necessary foundation.
**B**: valuable after that foundation. **C**: conditional on observed use.
Effort **S**: bounded extension of an existing operation; **M**: composition across modules;
**L**: new persistent metadata or multi-file recovery design. These are relative engineering sizes,
including compatibility and failure-path tests, not delivery estimates.

### Start, retrieve and resume

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A01 | B / S | Add structured initialization output with project path, revision, roots, document tokens and warnings. Reuse `allocate_project` and token helpers instead of requiring a follow-up context read just to obtain these fields. | Choose objective, target and workspace; ambiguous roots still need resolution. |
| A02 | A / M | Produce one bounded resume bundle: validated context, live handoff, an explicitly selected task, direct dependencies, selected design/spec sections and requested evidence entries. Currently validated context cannot combine with task projections. Reuse existing readers. | Select the focus and relevant sections when saved pointers are missing or ambiguous; resolve contradictions. |
| A03 | B / M | Support conditional reads using caller-provided document tokens: return unchanged markers and changed selected content, with per-source pagination. Avoid returning the same spec and notes on every lookup. | Say what context is still retained. After context loss, request a full needed selection rather than pretending an unchanged marker supplies its contents. |
| A04 | A / S | Add exact decision/finding entry lookup and section reads for architecture. `append_record` already creates entry IDs/anchors, but exact `--entry` retrieval currently exists only for command evidence. | Choose the relevant record; distinguish historical and current decisions. |

### Checkpoints and alignment records

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A05 | A / M | Generate checkpoint metadata and the resume prompt: current revision, source hashes, phase, task/evidence references, observed Git branch/commit/dirty paths; render and guardedly save `handoff.md`. Reuse returned tokens and avoid empty rewrites. | Supply next action, open decisions, partial-work interpretation and ownership observations. Git status cannot identify which person or agent made a change. |
| A06 | A / M | Compare checkpoint metadata with current canonical documents and return a bounded freshness report: changed, missing, unchanged or unknown. Include observed target changes separately. | Decide whether changes invalidate the saved next step. Matching hashes do not prove permission, stopped processes or semantically current evidence. |
| A07 | A / L | Save an alignment round in one named operation: caller-provided spec-section changes, dated decisions, optional design draft and continuation fields. Preflight every document token and return all resulting tokens/partial-write information. | Provide actual answers, unresolved questions, proposal and rationale. The script does not infer answers or agreement. |
| A08 | B / M, after A07 | Record an explicit confirmation against the exact proposal token/review ID, append its source/scope, update the design status and relevant spec link, and refresh continuation metadata. | Supply the real confirmation and judge its scope. Requirements confirmation must not become architecture agreement or effect authorization. |

### Planning, changes and task preparation

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A09 | A / M | Preview a small `update` patch with a compact task/status/dependency diff and validation findings. Reuse merge/default/validation code; current `commit --dry-run` requires a full candidate file. | Propose tasks, criteria, effects and dependency choices. Preview is useful for complex replans, not an extra mandatory call for every mutation. |
| A10 | B / M | Create a correction task from an identified terminal task: allocate a collision-free ID, link the superseded work in a finding, apply supplied criteria/outputs/dependencies, and return the assignment. | Decide the correction scope. Do not copy terminal status, old acceptance evidence, receipts or authorization into the new task as defaults. |
| A11 | A / M | Generate a change-impact candidate report from explicit task IDs: reverse dependency closure, affected terminal/nonterminal tasks, declared outputs, evidence and reviews. Show declared path overlaps where useful. Reuse graph construction. | Identify the initial affected work and actual semantic impact. Undeclared dependencies and v3 tasks without `reads` prevent claims of complete impact coverage. |
| A12 | B / L | Apply an agent-selected reconciliation batch: block/skip affected nonterminal tasks, add corrections, rewire dependencies, clear obsolete reasons, reopen selected reviews and save the decision/continuation pointers. Validate the whole candidate before the state commit. | Choose the dispositions, reuse of partial work, evidence applicability and authorization. Never mechanically invalidate everything downstream or rewrite terminal history. |
| A13 | B / M | Assemble a task/worker packet from the existing assignment projection plus explicitly selected spec sections, decision IDs, dependency findings, lesson assessments and write scope. Return one reusable bounded package. | Select applicable constraints and decide whether delegation is useful. Packaging does not dispatch or authorize a worker. |

### Execution, evidence and effects

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A14 | B / M | Record native-worker lifecycle events consistently: launch intent, host handle, assignment revision, scope and observed outcome; surface unresolved handles on resume. Reuse task-note append IDs and compact event projections. | Launch/observe/stop through the host and provide actual observations. Local scripts cannot verify an opaque host handle independently. |
| A15 | B / M | Run a caller-supplied list of verification argv arrays in order, record each real result, stop or continue on failure according to an explicit option, and return a compact evidence-ID summary. Reuse the current evidence runner. | Choose authorized checks and judge acceptance. Never execute the free-text task `verification` field, retry unknown effects automatically, or mark a task done because checks exited zero. |
| A16 | C / L | Optionally record fingerprints for explicitly selected checked files and declared environment/config inputs; report changes before evidence reuse. Hash relevant bounded inputs, not entire workspaces by default. | Define what the check depends on and whether reuse is justified. Matching selected fingerprints are insufficient to prove freshness for undeclared inputs or remote state. |
| A17 | B / M | Provide typed helpers for recording scoped authorization and external receipts, building correct field shapes, references and timestamps from supplied facts. Validate completeness and duplicates through existing guards. | Supply the actual permission source, authorized scope/time and observed receipt. Receipt recording must not itself perform or retry the external action. |
| A18 | B / L | Persist command-attempt outcomes for launch errors/timeouts and distinguish command completion from evidence-save failure. The current runner raises on timeout/launch failure without a selectable result. Return recoverable attempt IDs and partial output where available. | Interpret partial execution and decide recovery. Timeout/launch error is not a fabricated exit code; a timed-out parent does not certify all descendants stopped. |

### Review, closure and maintenance

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A19 | B / L | Record a delivery-review cycle: allocate the next cycle/path, render supplied findings/version/evidence, preserve prior cycles and update canonical review state. Support explicitly reopening acceptance. | Provide reviewer, findings, dispositions and actual acceptance. Passing tests or a created review file cannot establish acceptance. |
| A20 | B / S | Expose a structured closure-readiness report grouped by tasks, outputs, reviews, receipts, reflection, memory and index state. Extend existing close validation; do not invent a second gate implementation. | Resolve the findings and decide whether the delivered result satisfies the request. Use when needed, not as a compulsory extra preflight before every close. |
| A21 | A / M | Extend closure to produce a final continuation summary/prompt result from the committed state, avoiding a manual post-close handoff rewrite. Preserve truthful committed/partial-save/index-recovery results. | Supply reflection, limitations and next steps. A saved final note must not claim closure if `project.json` was not committed. |
| A22 | C / M | Generate requested report scaffolds, task/evidence tables, rooted citations and optional graph/accounting sections; check that local links and anchors exist. Reuse graph and evidence parsers/report checks. | Write conclusions, limitations and design commentary; inspect claim support and presentation. Do not generate reports unless needed or treat valid links as proof of claims. |
| A23 | C / M | Compose maintenance/cancellation state updates with supplied task changes and a continuation record; preserve history and return legal next transitions. Reuse `update`/transition validation rather than adding another state machine. | Choose maintenance versus successor scope or cancellation, resolve workers, and supply the reason. No automatic deletion, rollback or stop claim. |

### Memory and recovery

| ID | Priority / effort | Automate | Remaining agent input or judgment |
| --- | --- | --- | --- |
| A24 | A / S | Return memory-retrieval health alongside candidates: absent, no matches, available or failed, plus bounded diagnostics. `memory_candidates` currently returns `[]` for both no matches and caught lookup failures; agents are told to distinguish them. | Decide whether an unavailable lesson source matters to the next decision. Keep retrieval failures from preventing independent project work. |
| A25 | B / L | Record local lesson assessments and resolve selected staged items in a named memory operation. Fill source/topic tokens, save supplied dispositions, promote to selected destinations, preserve unresolved items and return destination receipts. Add retry identities so uncertain promotion responses do not duplicate incidents. | Judge applicability, rule quality, promotion versus reflection/discard, and supply revised rule text. Cross-project topics and project records need recoverable partial outcomes and existing lock order. |
| A26 | A / L | Standardize operation-result recovery: operation ID, document tokens, actual revision, completed steps and remaining repair. Extend retry-safe append behavior to composed operations; detect a landed state commit and rebuild only derived indexes instead of replaying mutations. | Resolve semantic conflicts and unknown command/external outcomes. Repair derived data only when requested/in scope; never silently steal locks or release legacy ownership. |

## Original implementation grouping

Prefer existing command extensions and shared Python operations. Names below are sketches, not
the current interface contract. Preserve small, typed inputs rather than a generic script that interprets an
arbitrary workflow specification.

1. **Context and checkpoint:** A02, A04–A06, A24. Extend `context`/`read`; add a small `checkpoint`
   operation taking agent-authored continuation fields. These remove repeated reads and regenerated
   metadata on virtually every substantial session. A01 is a small adjacent initialization change.
2. **Record composition and recovery:** A07, A08, A21, A26. Share a bounded multi-document operation
   foundation for round saves and finalization. Keep locks and commit semantics explicit. A named
   operation should return a truthful recovery result, not promise impossible cross-file atomicity.
3. **Replanning and reviews:** A09–A12, A19. Build read-only previews/impact reports before mutation
   helpers. Factor task-ID/cycle allocation and record rendering rather than duplicating them.
4. **Execution support:** A13–A15, A17, A18. Package assignments, record observed worker events and
   batch selected checks. This does not reintroduce the retired automatic executor or a scheduler.
5. **Measured extensions:** A03, A16, A20, A22, A23, A25. Introduce when real lifecycle traces show
   the recurring cost. Some are useful read-only improvements; fingerprint and shared-memory writes
   need more design and failure testing than their command syntax suggests.

## First operation contracts to make concrete

**Checkpoint:** agent provides next action, selected pointers, unresolved questions, partial-work
notes and observed ownership/effects, plus the expected state revision and handoff token. Script
reads current metadata, rejects conflicts, merges only explicit continuation updates, preserves
unresolved entries unless explicitly resolved, saves the document and returns its token and resume
prompt. Do not silently convert legacy free-form handoffs or discard fields the reader cannot parse.
Choose a small versioned metadata block or companion representation before implementation.

**Resume:** agent provides project, optional task and bounded source selections, with any previously
retained tokens. Script returns a consistent read bundle and stale/missing-source findings. Include
explicit omitted-content markers and targeted continuation selectors. Do not recursively follow
every Markdown link, load all task history, or choose work by executing text from a handoff.

**Round save:** agent provides a closed set of document operations with expected content tokens,
real source/confirmation text and an idempotency key. Script preflights all inputs, applies writes
under the project lock without nested lock acquisition, and reports any partial effects. If state
also changes, `project.json` remains the final canonical commit point; rebuild indexes afterward.
There must be a supported recovery path for interruption between document writes.

**Impact:** agent provides affected task IDs and optional rooted paths. Script returns the
transitive dependent set and referenced artifacts/evidence with a compact proposed-change preview.
It distinguishes graph-derived candidates from agent-selected invalidation; applying changes is a
separate explicit operation.

These four contracts can absorb several catalog entries without expanding routine skill instructions
by one procedure per opportunity.

## How to decide whether the automation helped

The current [lifecycle trials](../tests/plugins/research/project/test_workflow_automation.py) use
19 calls for ordinary execution, 21 with a routine correction, and 28 with interruption recovery.
They begin after initialization and count CLI arguments/stdin and output; they do not measure
implementation work, user reasoning, model reasoning or billed tokens.

Concrete opportunities within those fixtures:

- Interruption recovery currently reads validated context, handoff, architecture, one assignment
  and one evidence record separately. A bounded resume bundle can compose those **five reads into
  one**, subject to retaining all required information. This is a call-count opportunity, not a
  measured token saving.
- A checkpoint command may still consume one call, like today's `edit`; its main savings are the
  hashes/phase/revision/path metadata the agent no longer authors and keeps synchronized.
- Saving related agreement/decision/checkpoint records together reduces calls across a single
  answered round. It cannot combine two rounds separated by a real user decision.
- Closure plus a final handoff currently takes two mutations. A composed operation can make it one
  call while still distinguishing a successful state commit from a failed final-note/index write.

For each implementation, compare the same old/new scenario for calls, required instruction words,
agent-supplied input bytes, returned bytes, conflicts/retries and elapsed script time. Report actual
model/billed tokens only if measured. Include a large-history project, repeated routine corrections,
material replanning, absent/broken memory, and interrupted writes. A shorter call sequence with a
much larger context payload is not automatically an improvement.

Compatibility tests must exercise v3/v4 through both launchers, Python 3.9/stdlib-only execution,
stale tokens/revisions, malformed data, immutable history, ownership guards, real failed checks,
partial persistence and post-commit index recovery. Keep semantic acceptance and consent intact.
No new command should require migration of otherwise valid existing projects just to save bookkeeping.

The catalog favors automating repeated deterministic work. Scope/design choices, permission
interpretation, selecting checks, judging evidence and resolving uncertain effects remain supplied
decisions; the scripts make those decisions durable and consistently applied.
