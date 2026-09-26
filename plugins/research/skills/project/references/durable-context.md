# Keep project context durable

Save facts that change the next action, its constraints or rationale. Persistence is event-driven,
not background autosave. Use guarded commands from [commands.md](commands.md).

## Save at the point of use

Save consequential user input before dependent work, answered interview/review rounds before the
next question, and findings before switching work. Preserve sources, qualifications, rejected
alternatives and uncertainty; proposals remain pending. Routine corrections follow the short path
in the skill; invalidated assignments/agreement need
[execution-changes.md](execution-changes.md).

Before launching a long command or worker, save intent, affected paths and recovery details; append
its handle when available and its outcome when observed. Native workers follow
[task-workers.md](task-workers.md). Unknown ownership blocks conflicting writes.

## Put each fact in its owning record

| Information | Record |
| --- | --- |
| Current requirements and constraints | Canonical sections of `spec.md`. |
| Consequential decisions and confirmations | Dated `append ... decision` entries, with source, scope and superseded decision. |
| Design, alternatives and agreement | `architecture.md`, with review ID and truthful draft/agreed status. |
| Findings, partial work and worker recovery | `append ... finding --task <id>` into `tasks/<id>.md`. |
| Acceptance checks | `record-evidence`, retaining actual output and failures. |
| Task status, authorization and receipts | Guarded `project.json` operations. |
| Continuation context | `handoff.md`, linking owning records. |
| Prior/new lessons | Local assessments / `memory-staging.md`; see [memory-operations.md](memory-operations.md). |

Before tasks exist, keep findings in draft design or the note; link artifacts for lengthy material.
Never invent task IDs. Preserve history through superseding entries, not erasure.

## Keep the layout small and consistent

Create directories with their first useful file. Task notes are optional; definitions stay in
`project.json`. `artifacts/` holds durable supporting material and requested deliverables;
repository outputs belong in the target. Send state patches through stdin; keep scratch work in
temporary storage. Generate reports and graphs only when needed.

`execution/` is legacy storage; preserve it and consult [legacy-executor.md](legacy-executor.md)
only for old ownership/storage. Follow existing references on resume instead of renaming files.
Requested cleanup must preserve cited outputs, evidence, receipts and recovery records.

### Delivery-review checkpoints

Use delivery review only when required by the user, acceptance criteria or repository workflow.
Use [`workflow review`](automation-records.md) with reviewer, scope/version, findings/dispositions,
outcome and evidence; it allocates `reviews/review_NN.md` and aligns canonical review state.
File existence or passing tests do not establish acceptance. Preserve earlier cycles.
Requirements/design agreement stays in spec/architecture; implementation fixes use task notes and
new correction tasks for terminal work. Reopen acceptance invalidated by corrections as `pending`.

## Maintain the continuation note while working

Read [handoff-writing.md](handoff-writing.md) before writing continuation fields or a brief; apply
its provenance, verifier-status and preservation rules to routine checkpoints and closure too.

Keep `handoff.md` short: focus/next action, open decisions, partial work, relevant pointers,
agreement, current revision/spec/design tokens, and worker/command ownership or uncertain effects.
Mark it `Session state: working`, `waiting`, `blocked`, or `ready for handoff`.
These labels do not change canonical status or ownership.

Refresh after material continuation changes, before waiting/ending a turn or a risky operation.
Batch related saves; a routine task transition already captured in state/evidence needs no duplicate
narrative. Use [`workflow checkpoint`](automation-records.md) with the last token and continuation
fields; metadata is derived. Read only for missing context or conflicts.
Move lasting facts to owning records before replacing the note, retaining unresolved entries.

Write owning records first. Check every result: cross-document writes are not one transaction.
On conflict, reread and reconcile; on failure, pause dependent work and report unsaved essentials.
Active executor guards require legacy recovery, never bypassing.

For handoff or interruption recovery, use [session-handoff.md](session-handoff.md).
Reconcile records and ownership before writes; resume agreement/open questions without replaying
history. Change a ready note to `working` before continuing; its label proves no process stopped.
