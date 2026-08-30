# Project

`project` is a Claude skill for substantial projects that should remain resumable and
auditable across sessions. It keeps the specification, task state, evidence, reviews,
deliverables, and project history in a persistent workspace.

Use it through prompts with `/research:project`. Claude manages the workspace state and supporting
tools while preserving explicit authorization boundaries for destructive and external actions.

## Say where the workspace is

The skill never guesses the workspace root. It takes the first of these that exists, and asks if
none do:

1. a path you give it in the prompt;
2. the `RESEARCH_WORKSPACE` environment variable;
3. a question to you.

The current working directory is deliberately not a fallback. Guessing is what produces two
workspaces holding divergent copies of the same project, and no later validation can reconcile them.

Set the variable once and every session finds the same workspace:

```sh
export RESEARCH_WORKSPACE=/path/to/workspace
```

Or name it in the prompt when you want a different one:

```text
/research:project Use the workspace at ~/research/workspace and list its projects.
```

A workspace root that does not exist is an error rather than something created behind your back. To
start a genuinely new workspace, say so:

```text
/research:project Create a new workspace at ~/research/workspace and start a project there.
```

## Create a project

Describe the outcome, audience, important constraints, deliverables, and what success looks like:

```text
/research:project Create a new project to produce an onboarding guide for data analysts.
The audience is non-technical, the guide belongs in docs/onboarding.md, and every command must have
a verification step.
```

The skill discovers the relevant context, agrees the goal with you, creates a persistent project,
establishes its specification and plan, performs the work, and records verification evidence.

Agreement is a real step, not a formality. The skill hands alignment to the [`grill`](../grill/)
skill, which interviews you in rounds about the decisions that are actually unsettled, recommends an
answer to each one, and then states its understanding back and asks you to confirm it. Nothing
reaches the planning stage until you have. The confirmation and every decision behind it are written
into the project's `spec.md`, so a later session can see what was agreed and when — not just what
was built.

Review feedback that changes a requirement, rather than an implementation detail, goes back through
the same interview, scoped to the part it affects. Settled decisions stay settled.

New projects use a dated project directory under the workspace:

```text
workspace/
├── INDEX.md
├── reflection.md
└── YYYY-MM-DD-NNN/
    ├── project.json
    ├── spec.md
    ├── evidence.md
    ├── tasks/
    ├── artifacts/
    ├── reviews/
    └── reflection.md
```

`project.json` is the canonical source for project and task status. `INDEX.md` is generated from
that state and must not be edited by hand.

## Find, navigate, and resume projects

Use the project ID when you know it, or describe the deliverable when you do not:

```text
/research:project List the projects in this workspace and summarize their status.

/research:project Resume project 2026-08-28-001 and tell me what is complete, what remains,
and what you recommend doing next.

/research:project Show the current specification, task dependencies, deliverables, and
verification evidence for the onboarding-guide project.

/research:project Show the latest review and summarize which feedback was accepted.
```

The skill validates recorded state against the filesystem before relying on it. If multiple
projects could match, it asks which one to resume instead of silently creating a duplicate.

## Make additional changes

Identify the project or its deliverable and describe the desired change:

```text
/research:project Update the onboarding guide with the new access-request process. Keep the
existing audience and structure, and reopen the project that owns the guide.

/research:project Apply this feedback to project 2026-08-28-001: shorten the prerequisites,
add a troubleshooting example, and verify every internal link.

/research:project Correct the false verification record for task T04, preserve the original
history, and re-run the check.
```

Maintenance of the same deliverable reopens its completed project and appends tasks, decisions,
evidence, and reviews. A materially different objective, audience, output, owner, or lifecycle
creates a linked successor project instead.

State changes use revision-checked commits. Terminal task history is preserved, and completed
tasks are not rewritten.

## Review, deliver, and close

```text
/research:project Prepare the current project for review. Summarize the outputs, verification
results, unresolved items, and pending external actions. Do not publish anything yet.

/research:project Record this review feedback, update the current specification, and revise
the deliverable: <feedback>

/research:project Publish the approved onboarding guide to <destination>. This authorization
applies only to that publication.

/research:project Validate the project and close it when all required work, evidence, reviews,
and delivery receipts are complete.
```

The skill does not report a project as `DONE` while required work, outputs, verification, review
acceptance, authorization, or delivery receipts are missing.

Project statuses are `ALIGNING`, `PLANNING`, `EXECUTING`, `REVIEW`, `BLOCKED`, `DONE`, and
`CANCELLED`.

Destructive and external actions require authorization scoped to the exact action. External work
also records a durable receipt. Authorization does not carry over to a reopened project,
replacement task, or later delivery.

## Migrate an older project

Migration is never applied silently. Ask for a preview, then authorize it explicitly after review:

```text
/research:project Inspect project 2026-08-28-001 and preview its migration to schema v3.
Do not change the project yet.

/research:project Apply the reviewed schema-v3 migration to project 2026-08-28-001.
```

Schema-v2 migration preserves the previous canonical state as `project.v2.json`. Schema-v1
migration preserves legacy files and imports historical tasks as `TODO`; it does not guess whether
old work was completed. A v2 external task that was `RUNNING` is parked as `BLOCKED` for
authorization reconciliation rather than being pre-authorized.

## Good to know

- Use this skill for complex or multi-session work; ordinary one-turn edits usually do not need it.
- Set `RESEARCH_WORKSPACE` in your shell profile so no session has to ask where the workspace is.
- Expect to be asked questions before work starts, and expect to be asked to confirm the goal.
  Answering "your call" to a question is a valid answer; it is recorded as your decision.
- Describe the desired outcome and authorization boundary instead of editing `project.json` or
  `INDEX.md` yourself.
- Generated repository files stay in their requested repository paths; workspace-native notes and
  artifacts remain with the project.
- Keep outputs and evidence rooted explicitly as `workspace`, `target`, or `external` references.
- Do not store credentials, tokens, private keys, or unnecessary personal data in the workspace.
- If an index rebuild fails after a state commit, the commit remains valid; run the named
  `rebuild-index` recovery command rather than repeating the original mutation.

For the complete workflow and state model, see [`SKILL.md`](SKILL.md) and
[`references/workspace-schema.md`](references/workspace-schema.md).
