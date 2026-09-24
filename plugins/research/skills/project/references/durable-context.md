# Keep project context durable

Another session should be able to continue from project files without reconstructing the chat.
Persist information that changes the next action, its constraints, or the reason for a decision.
This is event-driven agent behavior, not background autosave; an interruption before a successful
write can still lose newly received information. Use existing project records and guarded commands.

## Save at the point of use

- After consequential user input, record the decision, correction, constraint, preference or scoped
  approval before work that depends on it. Include its date, source and relevant wording; preserve
  scope and qualifications. Label an unresolved interpretation as such. A suggestion is not consent.
  If planned/running work changes, follow [execution-changes.md](execution-changes.md) to propagate
  the feedback through assignments, dependencies and verification before affected work continues.
- After each answered interview/review round, save settled answers and the remaining questions
  before asking the next round. Save the proposal/version awaiting confirmation before waiting.
  This applies during `ALIGNING`, before tasks exist; do not wait for complete consensus to save.
- After a useful investigation or failed approach, save the conclusion, supporting references,
  uncertainty and what it rules out before changing work. Keep rationale for consequential rejected
  alternatives so a successor does not repeat them. Do not preserve every exploratory command.
  Stage reusable lessons under [memory-operations.md](memory-operations.md), with source evidence.
- Before a long operation or worker launch, save its intent, affected paths and recovery approach;
  record the returned handle as soon as available. After it settles, save its outcome. Follow
  [task-workers.md](task-workers.md) for native workers; stop for legacy executor ownership.
- Before ending a turn, waiting for input, switching tasks or preparing a restart, refresh the
  continuation note if material context changed. Batch closely related updates; no empty writes
  after acknowledgments or repeated rewrites of unchanged records are needed.

## Put each fact in its owning record

| Information | Record |
| --- | --- |
| Current requirements, constraints and user preferences affecting the outcome | Relevant canonical sections of `spec.md`. Keep unknowns explicit. |
| Decisions, corrections, confirmation scope and important rationale | Dated `append ... decision` entries in `spec.md`; identify what a correction supersedes. |
| Design, alternatives and their trade-offs | `architecture.md`, with draft/agreed status and review identifier preserved truthfully. |
| Task discoveries, failed approaches, partial changes and worker recovery | `append ... finding --task <id>` into task notes; link lengthy supporting artifacts. |
| Actual acceptance checks | `record-evidence`, with the real output and result. |
| Task state, dependencies, scoped authorization and external receipts | Existing guarded `project.json` operations; prose cannot grant authorization or finish a task. |
| Current focus, open questions, pending confirmations, hypotheses and next action | The current `handoff.md`, linking the owning records instead of copying them. |
| Lessons from other projects | Project-local assessments with source topic/token, rule, applicability and disposition; adopted consequences in spec/design/tasks. Handoff links what the next step needs. |
| New candidate lessons for other projects | `memory-staging.md`, triaged at closure into shared topics or project reflection. |

Before tasks exist, keep investigation summaries in draft design or the continuation note; link a
project artifact for lengthy findings. Never invent task IDs for notes or evidence commands.
Keep secrets and unnecessary personal information out. Record the user's actual instruction with
enough context to recover its meaning; do not archive the conversation or private reasoning.

## Keep the layout small and consistent

Create optional directories only when writing their first useful file. Their absence is normal;
consistency means the same purpose, not identical empty scaffolding in every project.

- `tasks/<id>.md`: concise findings and recovery notes, created by `append ... finding --task <id>`.
  No note is required for a task with nothing useful to add beyond its definition and evidence.
  Task definitions and status live only in `project.json`; do not maintain a second task plan here.
- `artifacts/`: durable supporting material and requested project deliverables. Link long logs,
  analyses and reusable verification scripts from the owning task note or evidence. Repository
  deliverables belong in the target checkout. Do not put disposable state patches, command payloads
  or copied transcripts here; send patches through stdin and use temporary storage for scratch work.
- `reviews/review_NN.md`: delivery-review cycles reflected in canonical `review` state. Requirements
  and architecture agreement belong in `spec.md` and `architecture.md`. Reviewing another repository
  as the project's task does not itself require a delivery-review cycle: keep findings in task notes
  or a requested artifact.
- `execution/`: legacy machine-owned storage, no longer created. Never put execution notes or
  handoffs here. Native worker recovery belongs in task notes. Read
  [legacy-executor.md](legacy-executor.md) only when inspecting old storage or ownership blockers.

Do not generate reports, HTML counterparts, graphs or review cycles just to populate the layout.
On resume, follow existing references; do not rename historical files to match today's convention.
Before any requested cleanup, check outputs, evidence, receipts and document links. Preserve cited
files and executor recovery state; an old filename or empty-looking record alone is not disposable.

### Delivery-review checkpoints

Use a separate delivery review only when required by the user, agreed acceptance criteria or the
repository workflow. Usually it follows implementation and verification, before closure; a staged
delivery can have an earlier checkpoint. Routine code inspection and tests are task work, not new
review cycles. The initial design review is recorded in `architecture.md`, not `reviews/`.

For a checkpoint, record date, reviewer (human or agent), scope, inspected commit/revision or
artifact version, findings and their disposition, outcome, and evidence links in
`reviews/review_NN.md`.
Use the next sequential cycle and keep canonical `review` state aligned; see
[workspace-schema.md](workspace-schema.md#review-state). Do not infer acceptance from a file's
existence or passing tests. Retain old reviews and identify what a later cycle supersedes.

Propagate accepted requirement changes to `spec.md` and design changes to `architecture.md`, with
source links. Findings that only require implementation fixes belong in task notes and correction
tasks. Follow [execution-changes.md](execution-changes.md) when scope or acceptance changes.

## Maintain the continuation note while working

Use `read <project-dir> handoff`, then `edit <project-dir> handoff --body-file -
--expected-sha256 <token-or-missing>`. Keep it short, normally under 600 words. Include:

- Date, current phase/task, project revision, specification/design tokens and agreement status.
- The immediate next action and the few file/section pointers needed to perform it.
- Open questions, choices already answered, pending confirmation and unverified hypotheses.
- Partial outputs, verification still needed, evidence references and unresolved effects.
- Known worker/command handles and observed states, including unknown ownership.

Label the note `Session state: working`, `waiting`, `ready for handoff`, or `blocked`. These are
agent-authored descriptions, not schema statuses or locks. An ordinary checkpoint can record active
workers; only [session-handoff.md](session-handoff.md) establishes readiness to yield. On resuming
work after a ready checkpoint, change its label to `working` before new work. Old readiness never
proves that its author or commands have stopped.

Write owning records first, then refresh the note with their current revision/tokens and pointers.
Keep unresolved entries until resolved; move lasting conclusions to their owning records before
replacing the note. Preserve decision history with a new superseding entry rather than erasing it.
Save new user input promptly instead of reconstructing it at the end of a long tool sequence.

Writes across documents are not one transaction. Check each result; on conflict, reread and
reconcile. On failure, report what remains unsaved and pause dependent work. Do not claim the
checkpoint is current just because an earlier write succeeded. If active executor ownership blocks
record updates, follow [legacy-executor.md](legacy-executor.md); do not bypass the guard or act on
an unsaved scope change. Include unsaved essentials in the user-facing response if persistence
remains blocked, so they can be carried into recovery.

## Recover after interruption

Read validated context and the current note even when no deliberate handoff occurred. Reconcile
stale hints against canonical records, recent relevant decision entries, partial files, evidence
and ownership observations. A crash between record writes may leave a decision saved but its current
specification or note outdated; reconcile those records before dependent work. Preserve valid
agreement and continue open questions from where they stopped. Missing information must stay
unknown until recovered or clarified, never inferred into approval or a successful result.

Use the ownership and uncertain-effect checks in [session-handoff.md](session-handoff.md) before
resuming writes. Load only what the next action needs. Do not turn routine resume into reading all
history, or require the user to repeat facts already saved.
