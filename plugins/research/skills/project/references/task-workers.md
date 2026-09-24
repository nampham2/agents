# Scoped native workers

Delegate investigation-heavy or independent work when its tool output would crowd the coordinator,
or a separate perspective materially helps. Keep small, closely related work together to reuse
discovery. Minimize total coordinator-plus-worker tokens; fresh context alone does not prove
savings. These instructions permit scoped delegation subject to host restrictions, not additional
effects or nested delegation. Sequential work remains the default.

## Assignment

After checking dependencies, authorization and ownership, record `RUNNING` and retrieve:

```sh
research-project workflow <project-dir> packet -
```

Select the task and needed sources using [packet input](automation-context.md). Supply constraints,
current decisions, input paths, findings, write scope and relationship to the authorized outcome.
Preserve lengthy requirements. Do not copy conversation history, the entire skill, or logs.
Workers can read relevant source and repository instructions beyond the input pointers.
Include applicable adopted-lesson summaries and pending checks from project records; worker
context omits shared-memory candidates. Return new reusable observations to the coordinator.

Use a fresh native agent: Claude's non-fork `general-purpose` Agent or Codex's spawn tool with
conversation inheritance disabled (`fork_turns: "none"` or `fork_context: false`, as its actual
schema supports). Preserve the configured model. If fresh context, observation, or stopping is
unavailable or delegation is prohibited, continue inline; do not launch a detached CLI workaround.

Use [`workflow worker-event`](automation-execution.md) for launch intent, then the observed host
handle/outcome in `tasks/<id>.md`. Keep this a current recovery note.
Native workers are not tracked by `execution_active`; never overlap legacy owners or edit a worker's
assigned files concurrently.

## Worker instructions

Complete the assigned outcome in the stated directory and scope, following repository instructions.
Ask the coordinator about missing decisions or scope changes. Do not delegate, start another project
lifecycle, modify canonical state/shared records/coordinator notes, commit, publish, or perform
destructive/external actions.

Perform development checks needed to reach a correct result. Return outcome, changed paths, actual
command exit codes, artifact locations, and unresolved issues concisely. Put long findings in an
assigned artifact. Report partial changes and uncertainty when interrupted; do not claim canonical
completion.

## Acceptance and recovery

For routine corrections under the same assignment, reuse the worker and check its result. Settle it
before taking over its files. Invalidated assignments/inputs need
[execution-changes.md](execution-changes.md); settle old work before replacing its assignment and
assess partial results against revised criteria.

Inspect results and relevant artifacts. Run required acceptance checks through `record-evidence`;
reuse existing recorded evidence only when command, scope and checked outputs remain applicable.
A worker summary is not command evidence. Avoid duplicate exploratory checks. Mark `DONE` only
after acceptance and record the worker as finished/stopped. Reuse a worker for corrections to its
assignment; use a fresh agent for a distinct task.

On resume or cancellation, inspect the recorded handle and partial outputs. Establish that the
worker and its commands stopped before takeover or redispatch. An unobserved launch does not prove
nothing ran. Unknown ownership blocks conflicting work; preserve the record and unresolved effects.
