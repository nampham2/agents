# Architecture review before task planning

Every project goes through this review after its initial requirements grill. Requirements, design,
and effort are one iterative agreement loop inside `ALIGNING`, not a one-way handoff. Task planning
starts only when both the agent and user understand and agree on the current proposal. Read-only
investigation and review artifacts are part of alignment; do not create implementation tasks to
stand in for unresolved design decisions.

## Prepare a concrete proposal

Read the current specification, confirmed decisions, and relevant existing code or other target
material. Establish facts from those sources and identify assumptions. Present a concrete design
for the user to react to, with recommendations and the consequential alternatives and trade-offs.
Review each applicable area in depth; explain why an area does not apply instead of silently
omitting it. For non-code projects, use the corresponding deliverable structure, responsibilities,
dependencies, and failure cases rather than inventing software components.

| Area | Shared understanding to establish |
| --- | --- |
| Requirements and boundaries | Goals and acceptance criteria served by the design, exclusions, constraints, and unresolved assumptions. |
| High-level modules | Each module's responsibility, ownership of state, boundaries, dependencies, and reasons for the split. Distinguish existing pieces to reuse from changes and new pieces. |
| Code organization | Proposed directories, files or packages, entry points, public interfaces, test locations, and mapping from modules to concrete paths and output roots. |
| Interfaces and flows | Inputs, outputs, contracts, data models, state transitions, and the important end-to-end paths across boundaries. |
| Edge cases and failure behavior | Relevant invalid or empty inputs, boundary conditions, partial failures, retries, duplicate operations, concurrency, compatibility, migration, access control, and resource limits. State expected behavior and recovery, not just a list of risks. |
| Verification and operation | How module contracts and end-to-end behavior will be checked, how failures are detected, and any rollout or recovery needs within scope. |
| Implementation effort | Work by module or workstream, dependencies, difficult integrations, testing and migration effort, uncertainty, and what could change the estimate. |

Prioritize diagrams when presenting the design, then use prose for rationale and details. Include
a high-level module/dependency diagram, a diagram of the key end-to-end flow, and a directory tree
mapping modules to proposed code locations. For non-code work, show the corresponding deliverable
structure. Prefer editable Mermaid diagrams embedded in `architecture.md` and fenced text for the
directory tree; keep labels consistent with actual module names and paths.

Add sequence diagrams for consequential interactions, state diagrams for lifecycle or concurrency
behavior, and data relationship or deployment diagrams where those relationships matter. Show
important error, recovery, and boundary paths, not only the happy path. Use several focused diagrams
when one overview would become unreadable. Explain how to read each visual and the decision it
supports; do not replace contract details or effort assumptions with unlabeled boxes and arrows.

Walk through the diagrams with representative success and failure scenarios so the user can
challenge boundaries and edge-case behavior. Update diagrams in the same review round as the
requirements or design they depict. Tie consequential choices back to requirements and explain
what the plausible alternatives cost or simplify.

## Make effort reviewable

Estimate ranges per module or workstream and for the whole project before creating canonical tasks.
State units, the assumed implementer and staffing, reuse assumptions, and confidence. Distinguish
implementation effort from elapsed time, including dependencies or external waiting when relevant.
Include design uncertainty, integration, tests, migration, and rollout where applicable; explain
what is excluded. Avoid precise promises unsupported by evidence.

If an unknown prevents a useful estimate or design choice, name it and propose the smallest
investigation or disposable prototype that would resolve it. Carry out work already authorized by
the user; request authorization only when needed for that specific action. Feed the result back
into requirements, architecture, and effort. Do not quietly defer a blocking architecture decision
to an implementation task. Residual uncertainty may remain only with explicit shared acceptance of
its impact and a clear validation or contingency approach.

## Iterate to agreement

1. Present the proposed architecture, its requirement assumptions, edge-case behavior, and effort.
   State the agent's assessment and any concerns; do not claim readiness while material decisions
   remain unresolved.
2. Collect the user's corrections and decisions. When these expose requirement gaps or trade-offs,
   return to the sibling grill skill for the affected branches. Preserve decisions that still hold.
3. Update requirements, design, scenarios, and estimates together. Explain what changed and what
   previous conclusions or confirmations it invalidates, then review the affected design again.
4. When the agent judges the design coherent and feasible, summarize the current requirements,
   architecture, code organization, edge cases, effort range and assumptions, and any accepted
   residual uncertainty. Ask the user to explicitly confirm this version as the basis for task
   planning. Wait; silence, a draft, or requirements-only confirmation is not architecture agreement.
5. Record the user's confirmation and its scope, and finalize the agreed `architecture.md`. Only
   then hand that document to task planning.

There is no fixed number of rounds. Revisit requirements and grill as often as the architecture
needs. If the user changes a material decision after confirmation, reopen the affected review and
obtain agreement on the revised proposal before planning or implementing affected work. Editorial
corrections within the agreed design do not require a new approval cycle.

## Produce the architecture document

Create `<project-dir>/architecture.md` during review and update it as the proposal evolves. It is a
required workspace deliverable before task planning, not an optional final report. The coordinator
maintains it with normal file editing tools; the CLI's `read` and `edit` document selectors do not
support `architecture`. Read the current file before editing and preserve unrelated changes.

The document must be understandable without the chat transcript and contain:

- Review identifier and status (`draft` or `agreed`), plus the actual confirmation date and source
  when agreed. Changing substantive content creates a new draft revision needing agreement.
- Requirements and scope summary linked to the current `spec.md`, constraints, and assumptions.
- High-level architecture: module responsibilities, dependency relationships, state ownership,
  and the module/dependency diagram used in review.
- Code and deliverable organization: concrete paths, roots, entry points, public interfaces, test
  locations, which pieces are reused, changed, or new, and the proposed directory tree.
- Interfaces, data models, and important success and failure flows across modules, including the
  end-to-end flow diagram and applicable sequence, state, data, or deployment diagrams.
- Edge cases and recovery: each relevant scenario's expected behavior, responsible module, and
  verification approach. Include relevant compatibility, migration, and operational considerations.
- Consequential alternatives, the chosen trade-offs and rationale, open questions, and explicitly
  accepted residual risks with their validation or contingency approach.
- Implementation effort by module or workstream and overall, ranges and units, staffing and reuse
  assumptions, dependencies, confidence, exclusions, and the main sources of uncertainty.

Keep requirements authoritative in `spec.md` and design details authoritative in `architecture.md`.
Preserve the seven specification sections: link the architecture revision and summarize its key
constraints under `Constraints and important assumptions`; list `architecture.md` as a `workspace`
deliverable under `Deliverables and roots`; keep acceptance criteria under `Success and verification
criteria` aligned with the design's scenarios. Use guarded specification edits and read complete
relevant sections when bounded context truncates them. Avoid copying the whole design into the spec.

## Persist agreement and resume

Append dated decisions for consequential review changes and a confirmation entry identifying the
agreed requirements and architecture revision, effort assumptions, and the user's actual response.
Use a simple review identifier such as A1, A2 in `architecture.md`, its specification link, and decisions
so later edits cannot silently reuse approval of a different proposal. Maintain current content in
place and preserve history; do not invent confirmation or overwrite earlier agreements.

On resume, read the linked architecture document and reuse agreement that still covers the current
proposal. A missing document or draft revision is not a completed gate. For an existing project with
no recorded agreement, complete the missing alignment before new planning or affected implementation;
preserve completed tasks and historical evidence. When revisiting a project already beyond
`ALIGNING`, pause affected work and honor legal status transitions rather than forcing an invalid
transition. The architecture review is a skill-level gate recorded in `architecture.md`, the
specification and decision history; the CLI does not verify conversational agreement or require
this new document for legacy workspace validity. It is separate from the optional delivery
`review` state, so do not mark that state accepted or fabricate delivery review files to represent it.

Agreement establishes the basis for planning. It does not grant new destructive or external-action
authorization; existing scoped user authorization still applies.
