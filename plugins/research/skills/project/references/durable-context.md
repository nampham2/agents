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
| Repository worktrees and their closure decisions | `workflow ... worktree` entries in `project.json`. |
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

Use delivery review only when the user, acceptance criteria or repository workflow require it, via
[`workflow review`](automation-records.md) with reviewer, scope/version, findings, outcome and
evidence; it allocates `reviews/review_NN.md` and aligns review state. File existence or passing
tests do not establish acceptance; preserve earlier cycles. Requirements/design agreement stays in
spec/architecture; fixes use task notes and correction tasks for terminal work, and acceptance a
correction invalidates reopens as `pending`.

## Maintain the continuation note while working

Read [handoff-writing.md](handoff-writing.md) before writing continuation fields or a brief,
including for routine checkpoints and closure.

Keep `handoff.md` short: next action, open decisions, partial work, pointers, agreement, current
revision/spec/design tokens, and worker/command ownership or uncertain effects. Mark it `Session
state: working`, `waiting`, `blocked` or `ready for handoff`; labels change neither canonical status
nor ownership. Refresh after material changes, before waiting or ending a turn, and before a risky
operation, batching related saves: a routine task transition already in state/evidence needs no
duplicate narrative. Use [`workflow checkpoint`](automation-records.md) with the last token and
continuation fields. Move lasting facts to owning records before replacing the note, keeping
unresolved entries.

Write owning records first and check every result: cross-document writes are not one transaction.
On conflict reread and reconcile; on failure pause dependent work and report unsaved essentials.
Active executor guards require legacy recovery, never bypassing.

For handoff or interruption recovery use [session-handoff.md](session-handoff.md): reconcile records
and ownership before writes, resume agreement and open questions without replaying history, and set
a ready note to `working` before continuing; its label proves no process stopped.
