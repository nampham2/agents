# Keep project context durable

Another session should be able to continue from project files without reconstructing the chat.
Persist information that changes the next action, its constraints, or the reason for a decision.
This is event-driven agent behavior, not background autosave; an interruption before a successful
write can still lose newly received information. Use existing project records and guarded commands.

## Save at the point of use

- After consequential user input, record the decision, correction, constraint, preference or scoped
  approval before work that depends on it. Include its date, source and relevant wording; preserve
  scope and qualifications. Label an unresolved interpretation as such. A suggestion is not consent.
- After each answered interview/review round, save settled answers and the remaining questions
  before asking the next round. Save the proposal/version awaiting confirmation before waiting.
  This applies during `ALIGNING`, before tasks exist; do not wait for complete consensus to save.
- After a useful investigation or failed approach, save the conclusion, supporting references,
  uncertainty and what it rules out before changing work. Keep rationale for consequential rejected
  alternatives so a successor does not repeat them. Do not preserve every exploratory command.
- Before a long operation or worker launch, save its intent, affected paths and recovery approach;
  record the returned handle as soon as available. After it settles, save its outcome. Follow
  [task-workers.md](task-workers.md) for native workers; use executor procedures when active.
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

Before tasks exist, keep investigation summaries in draft design or the continuation note; link a
project artifact for lengthy findings. Never invent task IDs for notes or evidence commands.
Keep secrets and unnecessary personal information out. Record the user's actual instruction with
enough context to recover its meaning; do not archive the conversation or private reasoning.

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
record updates, settle it through the executor protocol first; do not bypass the guard or act on an
unsaved scope change. Include unsaved essentials in the user-facing response if persistence remains
blocked, so they can be carried into recovery.

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
