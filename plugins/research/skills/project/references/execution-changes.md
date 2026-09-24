# Changes during execution

Use full reconciliation below when feedback invalidates assignments, inputs, dependencies,
authorization, acceptance criteria or agreement, including during delivery review.

## Routine corrections

When those remain valid, apply the correction directly. Settle a worker before changing its files;
save useful rationale/recovery context in task findings, then rerun affected acceptance checks.
No change identifier, impact map, lesson search, task blocking or intermediate handoff is required.
Refresh the live note at the next material continuation checkpoint. For terminal tasks, add a new
correction ID; never rewrite history. Reopen a delivery review as pending if its acceptance no
longer covers the result. Starting the correction from REVIEW returns the project to EXECUTING.

## Full reconciliation

Reopen requirements/design agreement only for material scope, outcome, interface, constraint or
architecture changes. A clear user decision settles that choice; ask only about unresolved
consequences or a revised proposal not yet agreed. Preserve unaffected decisions and work.

## Contain and record the change

Stop dispatching affected tasks or accepting results against their old assignment. Identify live
workers and commands that may write affected outputs or consume invalidated inputs. Use
[task-workers.md](task-workers.md) to settle native workers and
[legacy-executor.md](legacy-executor.md) for active legacy executor ownership. Observe completion
or request a stop and confirm termination before replacing assignments or taking over their files.
Retain partial outputs and actual results for assessment; completion under the old assignment is
not acceptance under the new one. Unknown ownership blocks conflicting work. Unaffected work may
continue only when its inputs, outputs and decisions are independent of the change.

As soon as guarded writes are available, append a dated decision recording the user's feedback,
its source and qualifications, what it supersedes, and what is still undecided. Give a consequential
change a short identifier such as C1 for task notes and the handoff to reference. Record a proposal
as pending, not approved. If executor ownership prevents saving, resolve it first and retain unsaved
essentials in the user-facing response; do not bypass guards or act on unsaved scope changes.

Update `handoff.md` immediately with the pending change, affected work that must not resume yet,
known activity and the next reconciliation step. Do not leave its old next action looking current
while reconciling the change. A `waiting` or `blocked` session label does not pause tasks.

## Assess the impact and agreement

Trace the change through the current specification, design, task dependencies and actual artifacts.
Include downstream consumers even when they were already completed. Record a concise impact map
in the decision/task notes: affected task IDs, outputs, workers, evidence/reviews, authorization,
and the intended disposition. Inspect partial changes before deciding what can be reused.

Reassess prior adopted lessons affected by the new direction, following
[memory-operations.md](memory-operations.md). Search the new problem where existing lessons no
longer cover it. Update project-local assessments and their design/task consequences; do not carry
a lesson forward merely because the previous handoff listed it. Preserve unaffected assessments.

Update relevant specification sections; retain previous decisions and append the correction.
For a material design change, follow [architecture-review.md](architecture-review.md): create the
revised draft, link its review identifier, explain consequences and confirm the affected proposal
before replanning or implementing it. Revisit only unresolved branches of [grill.md](grill.md).
Do not treat approval of the former design as approval of a different design or external effect.

Block affected nonterminal tasks with the actual reason while they await agreement or replanning;
include affected downstream tasks that could otherwise start. Keep independent work available.
Use project `BLOCKED` only when the whole project cannot progress, with at least one genuinely
`BLOCKED` task and no `RUNNING` tasks. Record the reason. Reopening alignment does not permit an
illegal `EXECUTING -> ALIGNING` transition; retain the phase when only part of the work is affected.
For a whole-project return to alignment, use `EXECUTING/REVIEW -> BLOCKED -> ALIGNING` when those
guards hold. If only completed tasks exist, retain the phase and record the agreement still needed;
do not invent blocked implementation tasks to force a transition or bypass design agreement.

## Reconcile the plan

After applicable agreement, use revision-checked updates to reconcile affected tasks together.
Record which old assignment each replacement or correction supersedes. Apply these rules:

| Existing work | Disposition |
| --- | --- |
| Unstarted task, same outcome | Update its scope, criteria, checks, rooted outputs and dependencies. |
| Running task, same outcome | Settle its worker/commands, inspect partial work, revise the assignment and resume only after reconciliation. |
| Nonterminal task whose outcome is withdrawn or replaced | Mark `SKIPPED` with the real reason; add a new task ID for a different outcome. Preserve its notes and outputs. |
| `DONE` or `SKIPPED` task | Keep the entire task record immutable. Add correction/replacement tasks for changed deliverables or discovered defects. |
| Downstream consumer | Update nonterminal dependencies to the revised prerequisite; add correction tasks for affected completed consumers. |

Never delete or reuse task IDs. Dependencies of `RUNNING` and `DONE` tasks must be `DONE`;
`SKIPPED` is not a satisfied prerequisite. A stopped task gaining an unfinished prerequisite must
stay `BLOCKED` or become `TODO`, not remain `RUNNING`. Clear `block_reason`/`skip_reason` when
leaving their statuses. Rewire dependencies to the needed correction task, not just the old `DONE`
task whose output is now obsolete. Check for cycles and reconcile the whole affected chain.

Review effect kind, scope, destination and existing authorization for each changed assignment.
Carry permission forward only where it still covers the action. Record new explicit authorization
from actual user instructions when present; otherwise leave the newly required permission pending.
Existing external effects and receipts remain facts: inspect uncertain outcomes before retrying.
A change of direction does not authorize deleting partial work, rolling back a deployment, or
performing another external action. If the objective becomes a different project, use the successor
guidance in [maintenance.md](maintenance.md) and make its scope and location explicit.

## Reconcile verification and resume

Preserve actual evidence entries, including former passing results. Identify in task notes which
checks no longer establish success because requirements, inputs, outputs or scope changed. On
nonterminal tasks, remove inapplicable references from the current acceptance evidence while
retaining the original log. Terminal history stays unchanged; correction tasks own the new checks.
Do not erase historical failures or relabel an old pass as a failed command.

Revise acceptance criteria and verification commands with the plan. Rerun affected checks and
reuse prior evidence only when its scope and checked outputs still apply. If required delivery
review acceptance no longer covers the deliverable, reopen that review as `pending`, preserving
its earlier records. Architecture agreement and delivery review remain separate gates.

When resuming correction work from `REVIEW`, `task ... start` transitions to `EXECUTING` while
preserving review state. If using `update` to start tasks instead, include `status: EXECUTING`.
Run `context <project-dir> --validate` after reconciliation. Resolve invalid dependencies, status
transitions and records before dispatch. Semantic impact and evidence applicability still need
agent assessment; a valid workspace cannot prove that the new direction has been fully propagated.
Issue workers updated assignments, constraints and revision; never let a stale assignment resume
merely because its process or host handle is available.

Refresh the handoff after the owning records and plan are saved. Include the change identifier,
new direction and superseded direction, affected task dispositions, preserved partial work,
obsolete evidence and new checks needed, agreement/authorization still pending, worker states,
current revision/source tokens and the exact next action. Link the impact map instead of copying
it. A handoff before reconciliation finishes must name the remaining steps and prohibited stale
actions; the successor completes reconciliation before affected execution.

For example, switching file storage to SQLite may leave the original file writer task `DONE` as
history. Add a SQLite implementation task and a correction task for an already-completed reader;
make unfinished integration depend on the replacements. Old file-storage checks remain in the log
but cannot establish SQLite acceptance. The handoff points to the revised design and new tasks,
and explicitly retires the earlier instruction to continue building file-storage consumers.
