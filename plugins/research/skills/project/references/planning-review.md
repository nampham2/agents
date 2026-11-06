# Planning review before task planning

Every project goes through this review after its requirements grill. Requirements, plan and
effort are one agreement loop inside `ALIGNING`; task planning starts only when agent and user
agree on the current proposal. Do not create implementation tasks for open decisions.

`planning.md` is the agreement: what is built, why, how much and in what order. The technical
design is `artifacts/architecture.md`, written after agreement by the first task and
Goldfish-reviewed before code starts ([design-document.md](design-document.md)). A project begun
before the split keeps its agreement in a root `architecture.md`, read as `planning.md`.

## Prepare a concrete proposal

Assess lessons with [memory-operations.md](memory-operations.md), reusing the requirements-stage
assessment; link applied lessons to concrete choices or checks.

Read the specification, decisions and relevant code or material; establish facts and name
assumptions. Present a concrete plan with recommendations and trade-offs, covering requirements and
boundaries; approach and key decisions; work breakdown and order; effort; verification; risks and
unknowns. For non-code projects use deliverables, dependencies and failure cases. Module, interface
and algorithm design belongs to the design document.

Lead with one Mermaid dependency diagram of the work breakdown, labeled with real names and paths,
then prose. Walk the user through success and failure scenarios. Update diagrams with
the plan.

For low effort with nothing open, a short planning document is enough: lists may replace the
diagram, and the proposal may use the combined `alignment` confirmation in [grill.md](grill.md).
Medium and high effort keep full coverage and separate confirmations.

## Make effort reviewable

Rate effort per workstream and overall as low, medium or high before creating tasks.
Never estimate in hours, days or other time units. Name external waits as dependencies.

- Low: one module or file family, existing patterns, no new interface, migration or external
  system, one or two tasks, no open unknowns.
- Medium: several modules or one new contract, new tests, some integration, a handful of tasks,
  one or two known unknowns with a planned check.
- High: cross-cutting change, new mechanism, migration, external systems or rollout, many tasks,
  material uncertainty. Propose splitting the project or an investigation first.

Overall is the highest workstream level unless justified.

If an unknown blocks a level or choice, name it and propose the smallest investigation or
disposable prototype that would resolve it, within existing authorization, and feed the result
back. A blocking decision found later, including by the design task,
reopens this review through [execution-changes.md](execution-changes.md); never settle it inside a
task. Residual uncertainty needs explicit shared acceptance and a validation or contingency
approach.

## Iterate to agreement

1. Present the plan, assumptions, edge-case behavior and effort, with your concerns; do not claim
   readiness while material decisions are unresolved.
2. Collect corrections. For requirement gaps, return to the [requirements grill](grill.md) for the
   affected branches and keep decisions that hold.
3. Update requirements, plan, scenarios and effort together, say what changed and which
   confirmations it invalidates, then review the affected parts.
4. When the plan is coherent and feasible, summarize it with assumptions and accepted residual
   uncertainty, and ask the user to confirm this version as the basis for task planning. Wait:
   silence, a draft, or requirements-only confirmation is not planning agreement.
5. Record the confirmation and its scope and finalize `planning.md`; only then hand it to task
   planning.

If the user changes a material decision after confirmation, reopen the affected review and obtain
agreement on the revision before affected work. Editorial corrections need no new approval.

For changes during execution follow [execution-changes.md](execution-changes.md): revised agreement
does not itself update assignments or make old evidence applicable.

## Produce the planning document

Create `<project-dir>/planning.md` during review and update it as the proposal evolves; it is
required before task planning. Read it with `read <project-dir> planning`
and write it whole with the guarded `edit ... planning` in [commands.md](commands.md), preserving
unrelated changes. It must stand without the chat transcript, with these sections
(`Template: v1`):

1. Header: title, review identifier (`A1`, `A2`), a status line `Status: draft` or `Status: agreed`
   near the top in any Markdown emphasis (validation reads the first such line), the agreement
   date and source once agreed, and the template version. Substantive changes make a new draft.
2. Requirements and boundaries linked to `spec.md`, with constraints and assumptions.
3. Approach and key decisions: the chosen approach and each alternative with its trade-off and the
   requirement it serves.
4. Work breakdown: milestones with `depends_on`, outputs and their roots, effects and verification
   per milestone, and the dependency diagram. Name the design profiles
   ([design-document.md](design-document.md)) when any task writes code. A task that writes to the
   target needs the draft MR or a recorded exemption first, or an earlier external task that opens
   it.
5. Effort per workstream and overall: implementer, reuse, confidence, exclusions, uncertainty.
6. Edge cases and recovery: case, expected behavior, owner and verification.
7. Verification plan: each success criterion mapped to a check and to its failing-first control.
8. Risks and unknowns: the smallest investigation for each blocking one, accepted residual risk
   with its validation or contingency.
9. Open questions.
10. Lessons applied, with rejection or deferral reasons.
11. Agreement record: the confirmation block the tool appends.

**Design task.** When any task writes code (`local_write` with a `target` output that is not `.md`,
`.rst` or `.txt`), the first task is the design task: no dependencies, output
`artifacts/architecture.md` under root `workspace`. Every later code task's success criteria include
"update `artifacts/architecture.md` and its status row". Validation warns, never blocks, when the
document is missing, lacks a section or lacks a row for a `DONE` code task.

Preserve the seven specification sections: link the planning revision and summarize its key
constraints under `Constraints and important assumptions`; list `planning.md` as a `workspace`
deliverable under `Deliverables and roots`; keep `Success and verification criteria` aligned with
the plan's scenarios. Do not copy the whole plan into the spec.

## Persist agreement and resume

Persist each review round before waiting or starting dependent work ([durable-context.md]
(durable-context.md)). Save rejected alternatives with the plan; keep unanswered questions and the
revision awaiting confirmation in the continuation note.

Append dated decisions for consequential changes and a confirmation entry naming the agreed
requirements and planning revision, effort assumptions and the user's actual response. Use the
review identifier (A1, A2) in `planning.md`, its spec link and the decisions, so approval cannot be
reused for another proposal; a later substantive edit makes the confirmation stale. Preserve
history, never invent confirmation.

On resume, read the linked planning document and reuse agreement that still covers the current
proposal; a missing or draft document is not a completed gate. A project with no recorded agreement
completes alignment before new planning or affected implementation. Pause affected work beyond
`ALIGNING`. Validation warns when a `PLANNING`, `EXECUTING` or `REVIEW` project lacks an agreed
planning document; a project created on or after the gate cutoff cannot leave `ALIGNING` until its
spec, agreed planning document and both confirmations are recorded. Neither proves agreement. The
gate is separate from the delivery `review` state; never mark that accepted or fabricate
review files.

Agreement grants no destructive or external-action authorization.
