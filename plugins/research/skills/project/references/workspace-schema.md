# Workspace schema v4

Consult this reference for migration, unusual state repairs, and exact field/transition rules.
Routine work uses `context` and `update`; see [commands.md](commands.md). This describes the stored
format, not a requirement to read the schema or use every optional feature on each session.

## Authority and writers

`project.json` is the only authoritative source for project and task status. Markdown files contain
specification, evidence, reviews, and notes but must not duplicate canonical statuses.

One coordinator is the sole writer of `project.json`, shared Markdown records, `INDEX.md`,
`MEMORY.md`, and `POSTMORTEMS.md`. Workers own only assigned non-overlapping target-file paths in
isolated Git worktrees and return commits and attestations to the coordinator. State updates use
guarded execution operations or `research-project commit` with an expected revision; direct edits
are unsupported.

`INDEX.md` is a deterministic cache generated from canonical state. A stale index is an error at
close but does not supersede `project.json`. `MEMORY.md` and `POSTMORTEMS.md` are generated the same
way and carry the same rule; see [Workspace root files](#workspace-root-files).

## Canonical v4 state

Every listed field is required except `predecessor` and the v4-only task `reads` declaration:

```json
{
  "schema_version": 4,
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
  "execution": {
    "protocol_version": 1,
    "coordinator_run": null,
    "ownership_generation": 0,
    "attempts": {}
  },
  "predecessor": "2026-08-20-001",
  "tasks": [
    {
      "id": "T01",
      "name": "Produce the first deliverable",
      "status": "TODO",
      "depends_on": [],
      "reads": ["relative/path/to/input"],
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

The v4-only `execution` object is canonical coordination state. `protocol_version` is a positive
integer. `coordinator_run` is null while no automatic pass owns the project; otherwise it is that
run's non-empty id. `ownership_generation` is a non-negative fencing generation. `attempts` maps
task ids to current attempt ids and must agree with running tasks and the durable execution journal.

`execution/config.json` is immutable generation configuration created after probing atomic links,
same-volume storage, case sensitivity, and a POSIX subprocess runner. Fresh projects create it
during initialization. The execution store under `execution/` holds immutable plans, grants,
attempts, worktrees, integration worktrees, runtime reports, and recovery records; it is protocol
state, not a place for hand-authored project notes.

The optional task `reads` field is an exhaustive list of target-relative input files for automatic
execution. An empty list explicitly declares a self-contained task that uses only its task text.
Omitting `reads` keeps the task valid but makes it `R-UNENUMERABLE`, so it follows the sequential
path. This field is accepted only by schema v4; schema-v3 task objects remain unchanged.

## Optional automatic task plans and workers

Sequential coordination is the default, including for v4. When explicitly selected,
`research-project run-auto <project-dir>` automatically considers READY tasks. Its default capacity
is two and can be changed with `--concurrency`. Admission requires:

- effect `none` or `local_write`;
- one or more non-directory file outputs rooted at `target`;
- an explicit exhaustive `reads` list, including an explicit empty list for a self-contained task;
- a clean Git target checkout;
- non-conflicting resolved read/write claims;
- separate read-only `test` or `[` verification commands whose subjects cover every required output;
- an available Claude Code or Codex CLI.

The coordinator resolves these fields into an immutable, content-hashed plan bound to the exact
project revision and task definition. Empty/generic instructions, no-op or composed checks,
workspace/external outputs, directory subjects, uncovered required outputs, unsafe worker arguments,
and protocol-owned paths are refused rather than inferred.

Each admitted worker runs non-interactively in a coordinator-created Git worktree and may modify
only the plan's declared files. `RESEARCH_PROJECT_WORKER=1` forbids nested coordination. The
coordinator seals the process tree, rejects undeclared or missing outputs, runs the checks, and
commits the isolated result. It serially cherry-picks all accepted worker commits into an
integration worktree, reruns all checks, confirms the target still matches its clean baseline, and
only then fast-forwards the target and commits canonical acceptance.

The JSON report partitions tasks into `completed`, `blocked`, `fallbacks`, and `deferred`.
Fallbacks carry precise refusal reasons and return to sequential coordinator execution; they are not
permission to weaken checks or authorization. Claim conflicts and capacity overflow are deferred to
a later pass. Failed workers, checks, outputs, or integration block their tasks and preserve durable
attempt records for inspection and recovery.

## Project lifecycle

Project statuses are `ALIGNING`, `PLANNING`, `EXECUTING`, `REVIEW`, `BLOCKED`, `DONE`, and
`CANCELLED`.

At the skill level, `ALIGNING` includes mandatory requirements grill and iterative architecture
review. Requirements, design, edge cases, and effort are revisited until agent and user agree;
record the agreement and finalize the required workspace `architecture.md` before task planning.
This uses specification and decision records, not a new JSON state or the delivery `review` field.
Validation warns, and never errors, when a `PLANNING`, `EXECUTING`, or `REVIEW` project has no
`architecture.md`, none with a recognisable `Status: draft` or `Status: agreed` line, or one still a
draft. `BLOCKED` is reachable from `ALIGNING` and is not checked; `DONE` and `CANCELLED` stay quiet
so history remains valid. The CLI cannot verify conversational agreement. See
[architecture-review.md](architecture-review.md) for the document contract and resume behavior.

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
starts. `BLOCKED` requires `block_reason`; `SKIPPED` requires `skip_reason`. Those fields are null
in other states.

Terminal tasks are immutable. When historical evidence is false, append a dated correction task and
decision rather than rewriting completed task history.

## Rooted outputs and evidence

Local paths are always relative, cannot contain `..`, and are rooted explicitly:

- `workspace`: relative to the project directory;
- `workspace_root`: relative to the directory holding every project — the parent of the project
  directory. Shared records belong to no single project: `INDEX.md`, `MEMORY.md`,
  `POSTMORTEMS.md`, and the topic files under `memory/` live there, and a task that rewrites one
  declares it under this root
  instead of misdeclaring it as `workspace` or leaving it undeclared. `memory-staging.md` belongs
  to its project and is declared under `workspace`, exactly like `evidence.md`;
- `target`: relative to `working_directory`;
- `external`: a valid HTTP(S) URL with a host or a non-empty durable identifier prefixed by
  `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:`, or `commit:`. The prefix set is
  closed so a mistyped one is refused rather than accepted; `commit:<sha>` exists because landing a
  change is a delivery whose durable identifier is the commit SHA.

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
Every `DONE` task has at least one evidence reference. Keep evidence concise; link large logs
instead of embedding them.

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

## Briefing, specification, and evidence files

`briefing.md` records the briefing step: what the user asked for, and what checking it established.
It is optional. `init --briefing` writes these five `##` sections as a skeleton:

```markdown
# Project title — briefing

## Stated requirements
## Verified facts
## Corrected assumptions
## Background
## Open questions for grill
```

Each skeleton body is one line beginning `_Not yet written`, which is how a briefing nobody has
written is distinguishable from one that is deliberately brief. From the moment a project leaves
`ALIGNING`, validation warns once per section it cannot find and once per section still holding only
that placeholder. Reworded headings are tolerated by a recogniser per section, as with the
specification headings.

These are warnings and never errors, at close as well as during execution, and a missing
`briefing.md` produces neither: the file postdates every project created before the briefing step,
so requiring it would invalidate valid history and block reopening a closed project for maintenance.
`briefing.md` is not in the list of files required non-empty at close.

The briefing is append-only in spirit: when the grill interview contradicts a fact it verified, the
correction is recorded as a dated decision in `spec.md` rather than by rewriting the briefing. Grill
reads it and does not write it.

`architecture.md` records the design the user agreed to before task planning. It is read whole with
`read <project-dir> architecture` and replaced whole with `edit <project-dir> architecture` under
the same content-token guard as `reflection.md`; resume context lists its token beside the other
documents. Its status line (`Status: draft` or `Status: agreed`) is what validation reads. Like the
briefing it warns and never errors, and it is not in the list of files required non-empty at close.

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

`--task <id>` requires that the id exists in `project.json`, which is what makes a heading in this
file traceable to a task. Closure work owned by no task — the report check below is the only case
today — is recorded with `--step <name>` instead, from a fixed vocabulary the tool holds rather than
free text. The two options are mutually exclusive: a reserved task id would have weakened the
existence guard for every ordinary recording, and a free-form label would have made the heading
unverifiable. A step entry is headed `## <name> — <command>` and is otherwise identical, real exit
code included.

Because no shell is interposed, a bare `|`, `&&`, `;`, or `>` among the arguments is literal text to
the command rather than a pipeline; that argument list is refused, and `-- bash -lc '<pipeline>'` is
how to ask for a shell. The entry itself is appended under the project lock and the file replaced
atomically, so a commit validating `evidence.md` never reads a half-written file and an interrupted
append cannot destroy the entries already there.

Redact credentials, tokens, private data, and unnecessary command output from every workspace file.

## The closing report

Reports are optional deliverables, not closure requirements. Each format can be requested
independently in the `artifacts/` directory that `init` already created; an unspecified format
defaults to Markdown:

```text
YYYY-MM-DD-NNN/
└── artifacts/
    ├── report.md            # The plain technical record
    └── report.html          # Only when HTML is requested
```

When both are requested they carry the same findings and may share generated content; nothing in
this plugin converts between them. Each requested file carries five sections (`##` in Markdown,
`<h2>` in HTML):

```text
## Summary
## What was done
## Findings and evidence
## Limitations and what was not proven
## Open work
```

The report is addressed to a reader who was not in the session. That reader is served by neither of
the two documents that already exist: `evidence.md` is a command log, and `reflection.md` is a
post-mortem addressed to future sessions. The report cites rather than measures — every figure in it
traces to `evidence.md`, an `artifacts/` file, or a receipt — because closure is not the time to run
a new measurement.

`research-validate <project-directory> --report-format markdown|html|both` requires and checks only
the selected formats. Bare `--report` retains the legacy paired check and cannot be combined with
`--report-format`. Each checked file needs written sections. Add `--report-profile concise` for
ordinary requested reports; `execution` (the legacy default when omitted) additionally requires a
task-graph subsection under What was done. HTML checks cover tags, resources, colour tokens, themes,
and chart labels and captions. See [report-design.md](report-design.md) for the shared content
contract and [report-html.md](report-html.md) only for HTML work. Mechanical checks do not establish
that prose is true or charts look right.

Validation severities for the report:

- **Warning** — an existing report that is unreadable, still at its placeholder, or missing
  sections, reported by `--close` and by validating a project already `DONE` or `CANCELLED`.
- **Not a finding at all** — the same conditions in `ALIGNING`, `PLANNING`, `EXECUTING`, `REVIEW`,
  or `BLOCKED`. A report cannot exist before the work it reports on does, which is why this rule
  differs from the briefing's; `briefing.md` warns from the moment a project leaves `ALIGNING`.
- **No ordinary report error** — in any status, including at close. The reasoning is the one stated
  for `briefing.md` above: requiring a new file at close would invalidate valid history and block
  reopening a closed project for maintenance. `report.md` and `report.html` are not in the list of
  files required non-empty at close.

Neither an absent report, an absent counterpart, nor an omitted optional task graph produces a
warning. Explicit `--report` or `--report-format` checks report errors for missing or invalid
selected files; record the command with `record-evidence --step report`. Required report
deliverables still belong in task outputs, where the normal completion guards enforce their
existence.

If a report is requested for `CANCELLED`, its summary states the
cancellation reason and `## Open work` carries what a successor would pick up; the rule that
cancellation must never be presented as successful completion continues to bind.

## Workspace root files

Beside the project directories, a workspace root holds two generated caches and one directory of
cross-project lessons. [memory-architecture.md](memory-architecture.md) specifies the layer in full;
this section states the part that is schema.

```text
workspace/
├── INDEX.md                 # Generated: every project, from canonical state
├── MEMORY.md                # Generated: pointers into memory/, plus one into POSTMORTEMS.md
├── POSTMORTEMS.md           # Generated: one line per project post-mortem; no budget
├── memory/
│   └── <slug>.md            # One cross-project lesson; any length
└── YYYY-MM-DD-NNN/
    ├── memory-staging.md    # Candidate lessons for this project, drained at close
    └── ...
```

`MEMORY.md` and `POSTMORTEMS.md` are regenerated in full under the workspace index lock, in the same
call that regenerates `INDEX.md`. Do not edit either; each says so in its own header. `MEMORY.md`
holds the topic pointers plus one line pointing at `POSTMORTEMS.md`; `POSTMORTEMS.md` holds one line
per project directory with a readable `reflection.md`, taking its titles and statuses from
`project.json`, never from headings inside the post-mortems, because canonical titles cannot drift
from canonical state and document headings demonstrably do.

`MEMORY.md` is the only memory file with a size budget: **120 lines and 12 KB**, whichever binds
first, to keep discovery cheap when needed. Memory is consulted on demand, not read every session.
It is measured in bytes, as
`len(content.encode("utf-8"))`. A topic file has no budget at all, since nothing loads one until a
pointer says it is relevant, and neither does `POSTMORTEMS.md`, which is read only once a reader has
decided a named project is worth opening. The budgeted file therefore holds only what a person can
merge or retire: a term that grows once per project and is never retired does not belong in it.

A topic file opens with frontmatter of exactly these six fields, followed by a body of any length:

| Field | Rule |
| --- | --- |
| `name` | Non-empty slug, lowercase and hyphenated; equals the filename without `.md` |
| `description` | Non-empty single line; this is what `MEMORY.md` shows |
| `kind` | One of `preference`, `environment`, `method` |
| `scope` | Non-empty free text saying when the lesson applies |
| `sources` | Comma-separated `YYYY-MM-DD-NNN` project ids; may be empty |
| `updated` | `YYYY-MM-DD` |

Unknown and duplicated fields are rejected, as everywhere else in this schema.

Validation severities:

- **Error** — `MEMORY.md` over either budget; a topic file whose frontmatter cannot be parsed; and,
  under `--check-index`, a `MEMORY.md` or `POSTMORTEMS.md` that disagrees with regeneration.
- **Warning** — a `sources` id naming no project in the root; a legacy flat `reflection.md` in the
  root; a `memory-staging.md` still holding staged lines at close.

Two properties are load-bearing rather than incidental. First, **a malformed topic file never blocks
a commit**: generation skips what it cannot parse and reports it, so a note nobody has finished
writing cannot refuse the record of work that is finished. Memory is advisory; `project.json` is
canonical. Second, **absence is never a finding**: a root with no `MEMORY.md` and no `memory/` is
valid, and so is every project in it, with or without a `memory-staging.md`. The same holds for
`POSTMORTEMS.md`.

The memory layer itself does not add canonical fields. Schema v4 exists for execution state; memory
continues to live beside projects and remains compatible with schema v3.

## Transactional updates

Do not write `project.json` directly. Prefer `update` with a small patch; it constructs the
candidate and derives `current_tasks`. For a full candidate, starting from revision `R`, run:

```sh
research-project commit <project-dir> <candidate.json> \
  --expected-revision R
```

A candidate may advance several tasks at once; there is no one-task-per-commit rule. Finishing one
task and starting the next is one commit, so a plan of `N` tasks costs about `N + 1` commits rather
than `2N` — subject to every status in the candidate being true at the moment it is written. Adding
`--dry-run` runs every check below, takes no lock, writes nothing, and leaves the revision alone;
the usual rejection it catches is `current_tasks` disagreeing with the set of `RUNNING` task ids.

The command:

1. obtains the project lock;
2. rejects stale revisions;
3. preserves immutable identity and terminal task history;
4. enforces project and task transitions;
5. sets revision to `R + 1` and updates the timestamp;
6. validates the candidate, including close invariants for `DONE`;
7. atomically replaces `project.json`;
8. regenerates `INDEX.md`, `MEMORY.md`, and `POSTMORTEMS.md` under the workspace index lock.

On conflict, reload and reconcile. A lock directory contains `owner.json`; inspect it before
manually removing a lock believed to be stale. Never automatically steal a lock.

Three locks exist, each scoped to what it protects: `.project.lock` in a project directory,
`.index.lock` at the root for regenerating both caches, and `.memory.lock` at the root for the
read-modify-write that amends a topic file. None is reentrant, so `.memory.lock` is never held
across a call that regenerates the caches.

## Compatibility and migration

The validator recognizes:

- schema v4 with the execution state and automatic executor described above;
- schema v3 with the transactional project guarantees in this document but no automatic executor;
- schema v2 with strict checks for its documented fields and a warning that concurrency and
  authorization guarantees are limited;
- schema v1 when `00_meta.yaml` and `02_task_plan.md` exist.

Migration is preview-only unless `--apply` is explicitly supplied. V2 application retains the old
state as `project.v2.json`. Pure v1 migration preserves legacy files and imports tasks as `TODO` in
an `ALIGNING` v3 project; it never guesses historical task completion.

Existing v3 projects never activate v4 implicitly. After explicit user approval, first establish
that every pre-v4 installation with write access to the workspace or target has been upgraded and
quiesced. Then run:

```sh
research-project enable-execution <project-dir> --expected-revision R \
  --legacy-writers-quiesced
```

The command probes the execution store, writes `execution/config.json`, and atomically commits the
v3 → v4 transition. Without the quiescence attestation it refuses with `R-LEGACY-WRITER`. There is
no automatic migration and no supported downgrade for that generation.

Unmigrated v1 closure requires `--allow-legacy-close`, a non-empty `reflection.md`, and a clear user
warning that task completion could not be validated canonically.

## Completion invariant

A v4 or compatible v3 project may be `DONE` only when:

- it contains at least one task;
- every task is `DONE` or justified `SKIPPED`;
- dependencies are valid, acyclic, and satisfied for every completed task;
- required outputs and evidence exist under their declared roots;
- every required review is accepted with evidence;
- every authorization-required completed task has scoped explicit authorization;
- every completed external task has a durable receipt;
- `spec.md`, `evidence.md`, and `reflection.md` are present and non-empty (`briefing.md`,
  `architecture.md` and the two report files under `artifacts/` are deliberately not required, and
  warn at most);
- required specification sections and numbered review files exist;
- canonical state, local files, and the generated `INDEX.md`, `MEMORY.md`, and `POSTMORTEMS.md`
  agree.

Run both close and index validation after the transactional `DONE` commit:

```sh
research-validate <project-dir> --close --check-index
```

The validator checks structural state, local files, references, and index derivation. The
coordinator must still inspect semantic correctness, accepted feedback, the truth of authorization
sources, and the validity of external receipts.
