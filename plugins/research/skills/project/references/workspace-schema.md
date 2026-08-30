# Workspace schema v3

Read this reference completely before creating, resuming, migrating, or closing an agentic
workspace project.

## Authority and writers

`project.json` is the only authoritative source for project and task status. Markdown files contain
specification, evidence, reviews, and notes but must not duplicate canonical statuses.

One coordinator is the sole writer of `project.json`, shared Markdown records, and `INDEX.md`.
Workers own only assigned non-overlapping output paths and return results to the coordinator. State
updates use `research-project commit` with an expected revision; direct edits are unsupported.

`INDEX.md` is a deterministic cache generated from canonical state. A stale index is an error at
close but does not supersede `project.json`.

## Canonical v3 state

Every listed field is required except `predecessor`:

```json
{
  "schema_version": 3,
  "project": "2026-08-28-001",
  "title": "Short project title",
  "status": "ALIGNING",
  "created": "2026-08-28T10:00:00+02:00",
  "updated": "2026-08-28T10:00:00+02:00",
  "working_directory": "/absolute/path/to/target",
  "revision": 0,
  "current_tasks": [],
  "review": {
    "cycle": 0,
    "required": false,
    "status": "not_required",
    "evidence": []
  },
  "cancellation_reason": null,
  "predecessor": "2026-08-20-001",
  "tasks": [
    {
      "id": "T01",
      "name": "Produce the first deliverable",
      "status": "TODO",
      "depends_on": [],
      "outputs": [
        {
          "root": "target",
          "path": "relative/path/to/output",
          "required": true
        }
      ],
      "success_criteria": "Observable definition of done",
      "verification": "Command, inspection, or review that demonstrates success",
      "evidence": [],
      "effect": {
        "kind": "local_write",
        "description": "Create the requested repository file"
      },
      "authorization": {
        "required": false,
        "status": "not_required",
        "scope": null,
        "source": null,
        "authorized_at": null
      },
      "receipts": [],
      "skip_reason": null,
      "block_reason": null
    }
  ]
}
```

Unknown fields are rejected so misspellings cannot silently disable a guarantee. Timestamps must be
timezone-aware ISO-8601 values. `working_directory` must be an existing absolute directory.

`revision` is a non-negative integer changed only by transactional commit. `current_tasks` is always
an array and must equal the set of tasks in `RUNNING` state.

## Project lifecycle

Project statuses are `ALIGNING`, `PLANNING`, `EXECUTING`, `REVIEW`, `BLOCKED`, `DONE`, and
`CANCELLED`.

Allowed transitions are:

```text
ALIGNING  → ALIGNING | PLANNING | BLOCKED | CANCELLED
PLANNING  → PLANNING | EXECUTING | BLOCKED | CANCELLED
EXECUTING → EXECUTING | REVIEW | DONE | BLOCKED | CANCELLED
REVIEW    → REVIEW | EXECUTING | DONE | BLOCKED | CANCELLED
BLOCKED   → BLOCKED | ALIGNING | PLANNING | EXECUTING | CANCELLED
DONE      → DONE | PLANNING
CANCELLED → CANCELLED
```

`EXECUTING → DONE` is valid only when no review checkpoint is required. Reopening maintenance work
uses `DONE → PLANNING`; terminal historical tasks remain immutable and new tasks receive new IDs.

State coherence rules:

- `ALIGNING`, `PLANNING`, `REVIEW`, `BLOCKED`, `DONE`, and `CANCELLED` have no running tasks.
- `BLOCKED` contains at least one blocked task.
- `CANCELLED` has a non-empty `cancellation_reason` and no running tasks.
- Other statuses have a null `cancellation_reason`.
- `DONE` satisfies every completion invariant below.

## Task lifecycle and dependencies

Task statuses are `TODO`, `RUNNING`, `DONE`, `BLOCKED`, and `SKIPPED`.

```text
TODO    → TODO | RUNNING | BLOCKED | SKIPPED
RUNNING → RUNNING | TODO | DONE | BLOCKED | SKIPPED
BLOCKED → BLOCKED | TODO | RUNNING | SKIPPED
DONE    → DONE
SKIPPED → SKIPPED
```

Task IDs are unique and never reused. Dependencies reference existing task IDs, contain no
duplicates or cycles, and are hard prerequisites: every dependency of a `RUNNING` or `DONE` task
must be `DONE`. A skipped dependency is not satisfied; replan or skip downstream tasks explicitly.

Every task has non-empty `name`, `success_criteria`, and `verification` strings, even before it
starts. `BLOCKED` requires `block_reason`; `SKIPPED` requires `skip_reason`. Those fields are null in
other states.

Terminal tasks are immutable. When historical evidence is false, append a dated correction task and
decision rather than rewriting completed task history.

## Rooted outputs and evidence

Local paths are always relative, cannot contain `..`, and are rooted explicitly:

- `workspace`: relative to the project directory;
- `target`: relative to `working_directory`;
- `external`: a valid HTTP(S) URL with a host or a non-empty durable identifier prefixed by
  `receipt:`, `deployment:`, `message:`, `purchase:`, or `publish:`.

Outputs use:

```json
{"root": "target", "path": "src/example.py", "required": true}
```

Required local outputs of `DONE` tasks must exist under the selected root. Resolution rejects paths
and symlinks that escape that root.

Evidence references use:

```json
{"root": "workspace", "path": "evidence.md", "anchor": "T01"}
```

`anchor` is a non-empty string or null. Local evidence files referenced by `DONE` tasks must exist.
Every `DONE` task has at least one evidence reference. Keep evidence concise; link large logs instead
of embedding them.

## Effects and authorization

Every task classifies its effect:

- `none`: read-only or reasoning work; `description` is null;
- `local_write`: reversible work within the requested target; description is required;
- `destructive`: deletion, overwrite, irreversible mutation, or similarly risky local action;
- `external`: publishing, deployment, messages, purchases, or mutations outside the local target.

Authorization has this shape:

```json
{
  "required": true,
  "status": "explicit",
  "scope": "Deploy release 42 to the staging service",
  "source": "User message dated 2026-08-28",
  "authorized_at": "2026-08-28T14:30:00+02:00"
}
```

Statuses are `not_required`, `pending`, `explicit`, `denied`, and `deferred`. Destructive and
external effects always set `required: true`. A required task cannot be `RUNNING` or `DONE` unless
status is `explicit` with non-empty scope, source, and timezone-aware timestamp. Non-required
authorization uses `not_required` and null source/timestamp.

Authorization is task- and action-specific. It does not carry across reopened work, replacement
tasks, or repeated delivery.

A completed external task also has at least one receipt:

```json
{
  "kind": "deployment",
  "value": "deployment:release-42",
  "destination": "staging/eu-west",
  "timestamp": "2026-08-28T14:35:00+02:00"
}
```

All receipt fields are non-empty and the timestamp is timezone-aware. General evidence is not a
substitute for the receipt.

## Review state

Project review contains:

- `cycle`: non-negative integer matching sequential `reviews/review_NN.md` files;
- `required`: whether successful closure depends on acceptance;
- `status`: `not_required`, `pending`, `accepted`, or `recorded`;
- `evidence`: rooted references to review or acceptance records.

`accepted` records a required checkpoint. `recorded` preserves a review imported from an older
schema without claiming acceptance. Both require a positive cycle and evidence. When review is
required, a project cannot close until status is `accepted`.

Every cycle from 1 through the current cycle has a non-empty, sanitized review file. Update the
current specification for all accepted requirement changes.

## Specification and evidence files

`spec.md` has two non-empty sections:

```markdown
# Project title

## Current specification

The authoritative objective, audience, scope, constraints, assumptions, success criteria,
deliverables, and authorization state.

## Decision history

- YYYY-MM-DD — Decision or accepted change, with source when useful.
```

Current requirements are maintained in place; decision history is append-only.

`evidence.md` records concise milestone evidence. Entries for commands are written by
`research-project record-evidence <project-directory> --task <id> -- <command>`, which runs the
command with no shell and appends what it observed:

~~~markdown
# Evidence

## T01 — uv run pytest -q

- Recorded: YYYY-MM-DDTHH:MM:SS+00:00
- Working directory: /path/from/project.json
- Exit code: 0 (passed)

stdout (tail):

```
[N earlier line(s) elided]
...the last lines of output...
```
~~~

The exit code in the file is the process's real exit code, and a non-zero one is written down as
`FAILED`; the command itself exits non-zero and says it is not recording a pass. Prose belongs in a
separate `### T01 — notes` section below the recorded entry — outputs, limitations, why a failure
was expected — so that what a command did and what a coordinator concluded from it stay
distinguishable.
Never edit a recorded entry to make it agree with a conclusion.

Only evidence that no command produced, such as an external delivery receipt, is written by hand.

Redact credentials, tokens, private data, and unnecessary command output from every workspace file.

## Transactional updates

Do not write `project.json` directly. Starting from revision `R`, construct a complete candidate and
run:

```sh
research-project commit <project-dir> <candidate.json> \
  --expected-revision R
```

The command:

1. obtains the project lock;
2. rejects stale revisions;
3. preserves immutable identity and terminal task history;
4. enforces project and task transitions;
5. sets revision to `R + 1` and updates the timestamp;
6. validates the candidate, including close invariants for `DONE`;
7. atomically replaces `project.json`;
8. regenerates `INDEX.md` under the workspace index lock.

On conflict, reload and reconcile. A lock directory contains `owner.json`; inspect it before manually
removing a lock believed to be stale. Never automatically steal a lock.

## Compatibility and migration

The validator recognizes:

- schema v3 with all guarantees in this document;
- schema v2 with strict checks for its documented fields and a warning that concurrency and
  authorization guarantees are limited;
- schema v1 when `00_meta.yaml` and `02_task_plan.md` exist.

Migration is preview-only unless `--apply` is explicitly supplied. V2 application retains the old
state as `project.v2.json`. Pure v1 migration preserves legacy files and imports tasks as `TODO` in
an `ALIGNING` v3 project; it never guesses historical task completion.

Unmigrated v1 closure requires `--allow-legacy-close`, a non-empty `reflection.md`, and a clear user
warning that task completion could not be validated canonically.

## Completion invariant

A v3 project may be `DONE` only when:

- it contains at least one task;
- every task is `DONE` or justified `SKIPPED`;
- dependencies are valid, acyclic, and satisfied for every completed task;
- required outputs and evidence exist under their declared roots;
- every required review is accepted with evidence;
- every authorization-required completed task has scoped explicit authorization;
- every completed external task has a durable receipt;
- `spec.md`, `evidence.md`, and `reflection.md` are present and non-empty;
- required specification sections and numbered review files exist;
- canonical state, local files, and generated `INDEX.md` agree.

Run both close and index validation after the transactional `DONE` commit:

```sh
research-validate <project-dir> --close --check-index
```

The validator checks structural state, local files, references, and index derivation. The coordinator
must still inspect semantic correctness, accepted feedback, the truth of authorization sources, and
the validity of external receipts.
