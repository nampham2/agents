# Architecture review before task planning

Every project goes through this review after its requirements grill. Requirements, design and
effort are one iterative agreement loop inside `ALIGNING`; task planning starts only when agent and
user agree on the current proposal. Read-only investigation and review artifacts belong to
alignment; do not create implementation tasks to stand in for unresolved design decisions.

## Prepare a concrete proposal

Use [memory-operations.md](memory-operations.md) to assess lessons relevant to the design, reusing
the requirements-stage assessment where it applies. Link applied lessons to concrete design choices
or checks and record rejection or deferral reasons.

Read the current specification, confirmed decisions and the relevant existing code or material.
Establish facts from them and name assumptions. Present a concrete design for the user to react to,
with recommendations and the consequential alternatives and trade-offs. Review each applicable area
in depth and say why an area does not apply instead of omitting it. For non-code projects use the
deliverable structure, responsibilities, dependencies and failure cases, not invented components.

Review each of these in depth, establishing the shared understanding the final document records:
requirements and boundaries; high-level modules (responsibility, state ownership, dependencies,
reuse versus change); code organization; interfaces and flows; edge cases and failure behavior;
verification and operation; implementation effort.

Lead with diagrams, then prose for rationale. When the design has more than one module or flow,
include a module/dependency diagram, a diagram of the key end-to-end flow, and a directory tree
mapping modules to code locations; a single-module design may use a short list and says so. For
non-code work show the deliverable structure. Prefer editable Mermaid in `architecture.md` and
fenced text for the tree, with labels matching actual module names and paths.

Add sequence diagrams for consequential interactions, state diagrams for lifecycle or concurrency,
and data or deployment diagrams where those relationships matter. Show error, recovery and boundary
paths, not only the happy path, using several focused diagrams rather than one unreadable one.
Explain how to read each visual and the decision it supports; never replace contract details or
effort assumptions with unlabeled boxes and arrows.

Walk through the diagrams with representative success and failure scenarios so the user can
challenge boundaries and edge cases. Update diagrams in the same round as the design they depict,
and tie consequential choices to requirements and to what the alternatives cost or simplify.

For low effort with nothing open, a short architecture is enough: lists may replace diagrams, and
the proposal may be confirmed with the combined `alignment` confirmation described in
[grill.md](grill.md). Medium and high effort keep the full coverage and separate confirmations.

## Make effort reviewable

Rate effort per module or workstream and overall as low, medium or high before creating tasks.
Never estimate in hours, days or other time units; they are unreliable and read as promises. Name
external waits as dependencies, not estimates.

- Low: one module or file family, existing patterns, no new interface, migration or external
  system, existing tests plus small additions, one or two tasks, no open unknowns.
- Medium: several modules or one new contract, new tests or fixtures, some integration, a
  handful of tasks, one or two known unknowns with a planned check.
- High: cross-cutting change, new mechanism, migration or compatibility work, external systems or
  rollout, many tasks, material uncertainty. Propose splitting the project or an investigation
  first.

Overall is the highest workstream level unless justified. State implementer, reuse and confidence;
cover design uncertainty, integration, tests, migration and rollout, and say what is excluded.

If an unknown blocks a useful level or design choice, name it and propose the smallest
investigation or disposable prototype that would resolve it, within existing authorization. Feed the
result back into requirements, architecture and effort; never defer a blocking architecture decision
to an implementation task. Residual uncertainty needs explicit shared acceptance of its impact and a
validation or contingency approach.

## Iterate to agreement

1. Present the architecture, its requirement assumptions, edge-case behavior and effort, with your
   assessment and concerns; do not claim readiness while material decisions are unresolved.
2. Collect the user's corrections. When they expose requirement gaps or trade-offs, return to the
   [requirements grill](grill.md) for the affected branches and keep decisions that still hold.
3. Update requirements, design, scenarios and effort together, say what changed and which earlier
   conclusions or confirmations it invalidates, then review the affected design again.
4. When the design is coherent and feasible, summarize requirements, architecture, code
   organization, edge cases, effort, assumptions and any accepted residual uncertainty, and ask the
   user to confirm this version as the basis for task planning. Wait: silence, a draft, or
   requirements-only confirmation is not architecture agreement.
5. Record the confirmation and its scope and finalize the agreed `architecture.md`; only then hand
   it to task planning.

Rounds are unbounded. If the user changes a material decision after confirmation, reopen the
affected review and obtain agreement on the revised proposal before planning or implementing
affected work. Editorial corrections within the agreed design need no new approval.

For changes during execution follow [execution-changes.md](execution-changes.md): revised agreement
does not by itself update assignments or make old evidence applicable.

## Produce the architecture document

Create `<project-dir>/architecture.md` during review and update it as the proposal evolves; it is a
required workspace deliverable before task planning. Read it with `read <project-dir> architecture`
and write it whole with the guarded `edit ... architecture` in [commands.md](commands.md): a stale
token is refused, so read before editing and preserve unrelated changes. The document must stand
without the chat transcript:

- Review identifier and a status line `Status: draft` or `Status: agreed` near the top, in any
  Markdown emphasis; validation reads the first such line. Add the confirmation date and source when
  agreed. Substantive changes create a new draft needing agreement.
- Requirements and scope summary linked to `spec.md`, with constraints and assumptions.
- Module responsibilities, dependencies, state ownership and the module/dependency diagram.
- Concrete paths, roots, entry points, public interfaces, test locations, reuse versus change, and
  the directory tree.
- Interfaces, data models and the important success and failure flows, with the end-to-end diagram
  and any sequence, state, data or deployment diagrams.
- Edge cases and recovery: invalid or empty inputs, boundaries, partial failures, retries,
  duplicates, concurrency, compatibility, migration, access control and resource limits, each with
  expected behavior, responsible module and verification, not a list of risks.
- Verification and operation: how contracts and end-to-end behavior are checked, how failures are
  detected, and any rollout or recovery within scope.
- Consequential alternatives, trade-offs and rationale, open questions, and accepted residual risks
  with their validation or contingency.
- Effort by module or workstream and overall, with reuse, dependencies, confidence, exclusions and
  sources of uncertainty.

Keep requirements authoritative in `spec.md` and design details in `architecture.md`. Preserve the
seven specification sections: link the architecture revision and summarize its key constraints under
`Constraints and important assumptions`; list `architecture.md` as a `workspace` deliverable under
`Deliverables and roots`; keep `Success and verification criteria` aligned with the design's
scenarios. Use guarded specification edits, reading complete sections when context truncates them,
and do not copy the whole design into the spec.

## Persist agreement and resume

Persist each review round before waiting or starting dependent work ([durable-context.md]
(durable-context.md)). Save rejected alternatives with the design, and keep unanswered questions and
the revision awaiting confirmation in the continuation note. A saved proposal stays a draft until
the user agrees.

Append dated decisions for consequential changes and a confirmation entry naming the agreed
requirements and architecture revision, effort assumptions and the user's actual response. Use a
review identifier such as A1, A2 in `architecture.md`, its spec link and the decisions, so approval
cannot be reused for a different proposal; a later substantive edit makes the confirmation stale and
it must be recorded again. Maintain current content in place, preserve history, never invent
confirmation.

On resume, read the linked architecture and reuse agreement that still covers the current proposal;
a missing document or draft is not a completed gate. A project with no recorded agreement completes
alignment before new planning or affected implementation, keeping completed tasks and evidence.
Pause affected work in a project beyond `ALIGNING` and honor legal transitions. Validation warns
when a `PLANNING`, `EXECUTING` or `REVIEW` project lacks an agreed `architecture.md`; a project
created on or after the gate cutoff cannot leave `ALIGNING` until its spec, agreed architecture and
both confirmations are recorded. Neither proves agreement. The gate is separate from the optional
delivery `review` state; never mark that accepted or fabricate review files.

Agreement establishes the basis for planning. It does not grant new destructive or external-action
authorization; existing scoped user authorization still applies.
