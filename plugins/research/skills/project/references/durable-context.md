# Keep project context durable

Save facts that change the next action, its constraints or rationale. Persistence is event-driven.
Use guarded commands from [commands.md](commands.md).
Write each record as [writing-rules.md](writing-rules.md) says.

## Save at the point of use

Save consequential user input before dependent work, answered interview/review rounds before the
next question, and findings before switching work. Keep sources, rejected alternatives and
uncertainty. Proposals stay pending. Invalidated assignments/agreement need
[execution-changes.md](execution-changes.md); routine corrections use the skill's short path.

Before a long command or worker, save intent, affected paths and recovery details; append its handle
and outcome. Native workers follow [task-workers.md](task-workers.md). Unknown ownership blocks
conflicting writes.

## Put each fact in its owning record

| Information | Record |
| --- | --- |
| Requirements and constraints | Canonical sections of `spec.md`. |
| Consequential decisions and confirmations | Dated `append ... decision` entries with source, scope and superseded decision. |
| Plan, alternatives and agreement | `planning.md`, with review ID and true draft/agreed status. |
| Technical design | `artifacts/architecture.md`. |
| Findings, partial work, worker recovery | `append ... finding --task <id>` into `tasks/<id>.md`. |
| Friction with the skill or the tool | `append ... friction --kind <kind> --pointer <ref>` into `friction.md`. |
| Acceptance checks | `record-evidence`, with actual output and failures. |
| Task status, authorization, receipts | Guarded `project.json` operations. |
| Worktrees and closure decisions | `workflow ... worktree` entries in `project.json`. |
| Continuation context | `handoff.md`, linking records. |
| Lessons | Local assessments, `memory-staging.md`; see [memory-operations.md](memory-operations.md). |

Before tasks exist, keep findings in draft design or the note; link artifacts for long material.
Never invent task IDs. Supersede entries; never erase history.

## Keep the layout small and consistent

Create a directory with its first useful file. Task notes are optional; definitions stay in
`project.json`. `artifacts/` holds supporting material and requested deliverables; repository
outputs belong in the target. Keep scratch work in temporary storage.

`execution/` is legacy storage: preserve it; consult [legacy-executor.md](legacy-executor.md)
only for old ownership. Cleanup must keep cited outputs, evidence, receipts and recovery records.

### Delivery-review checkpoints

A Goldfish round ([goldfish-review.md](goldfish-review.md)) is not delivery review. Use delivery
review only when the user, acceptance criteria or repository workflow require it, via
[`workflow review`](automation.md) with reviewer, scope/version, findings, outcome and
evidence; it allocates `reviews/review_NN.md` and aligns review state. A file or passing tests do
not establish acceptance. Keep earlier cycles. Agreement stays in spec/planning. Fixes use task
notes, and correction tasks for terminal work; a correction that invalidates acceptance reopens it
as `pending`.

## Maintain the continuation note while working

Read [handoff-writing.md](handoff-writing.md) before writing continuation fields or a brief.

Keep `handoff.md` short: next action, open decisions, partial work, pointers, agreement, tokens,
and ownership or uncertain effects. Mark it `Session state: working`,
`waiting`, `blocked` or `ready for handoff`; labels change neither status nor ownership.
Refresh after material changes and before waiting, ending a turn or a risky operation; a routine
transition already in state/evidence needs no narrative. Use
[`workflow checkpoint`](automation.md) with the last token and continuation fields. Move
lasting facts to owning records before replacing the note.

Write owning records first and check every result: cross-document writes are not atomic. On
conflict reread and reconcile; on failure pause dependent work and report unsaved essentials.
Active executor guards require legacy recovery.

For handoff or interruption recovery use [session-handoff.md](session-handoff.md): reconcile records
and ownership before writes, resume agreement and open questions without replaying history, and set
a ready note to `working`; its label proves no process stopped.
