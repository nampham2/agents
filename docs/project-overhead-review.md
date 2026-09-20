# Project skill overhead review — 2026-09-18

The main cost was mandatory agent procedure, not Python execution time. The previous entrypoint
contained 6,172 words and required a schema read, briefing, interview confirmation, graph presentation,
automatic-worker attempt, memory discovery, and paired closing reports. State updates required a
complete candidate JSON even to change one task. These costs compounded as projects accumulated history.

This review inspected the configured research workspace read-only: its index and 65 canonical
project records (940 tasks, 1,387 revisions), targeted reflections, report sizes, and the two
`execution/runtime/automatic-run.json` files found there. Counts and file sizes are observations;
they do not measure tokens billed, agent time, or the time spent in each lifecycle step.

## Evidence behind the changes

Paths below are relative to the configured workspace; project contents were not copied into this
repository and no live project was changed.

| Evidence | Implication and change |
| --- | --- |
| `2026-09-14-002/reflection.md`, friction points: constructing candidates for short task transitions cost most of a round trip. | Add `update`: the agent supplies changed fields; code assembles and commits the complete candidate. |
| `2026-09-08-002/reflection.md`, dry-run finding: `current_tasks` disagreed with the task statuses. | Derive `current_tasks` in code. Preserve revision checks instead of requiring a routine extra dry run. |
| `2026-09-16-007/reflection.md`: the executor added nothing to a seven-task chain. Its runtime report records zero completions and `R-UNENUMERABLE`. `2026-09-18-001` also records zero completions and three such fallbacks. | Default to sequential execution; omit mandatory executor attempts. Keep opt-in execution and recovery support. Only two stored reports were found; this is not proof that no other dispatch ever occurred. |
| `2026-08-28-002`: 62 tasks, revision 98; `project.json` 84,802 bytes, `spec.md` 72,027, `evidence.md` 131,488, `reflection.md` 53,873. | Add compact resume context and retrieve individual tasks. Stop requiring blanket reads of history and memory. |
| `2026-09-16-007/artifacts/report.{md,html}` total 56,098 bytes; `2026-09-18-001` totals 56,844 bytes. `2026-09-17-005/reflection.md` records repeated report revisions as tasks were added. | Reports become requested deliverables. Remove mandatory duplicate authoring and chart/graph presentation from closure. |
| `2026-09-16-005/reflection.md` and `2026-09-17-005/reflection.md` record report checks run from the wrong directory. | Keep the working-directory rule prominent and use absolute project paths in command examples. |
| `2026-09-17-004/reflection.md` credits the interview with useful scope decisions; other reflections also report valuable alignment. | Retain the interview for substantial uncertainty. Remove automatic re-confirmation for a clear request or clear feedback. |

## Implementation

The entrypoint now describes the default path; optional command details live in `references/commands.md`.
Schema, memory, report design, and execution recovery references are loaded only for those operations.
New projects still use v4, existing v3 projects remain supported, and no migration is needed.

`context` returns counts, a bounded active-task summary, ready IDs, review/execution state, and the
current specification excerpt. It does not return completed history or logs. `--task` retrieves a
complete task, including its requirements and authorization. Truncated specifications and omitted
tasks are explicit, so the summary is not mistaken for the whole project. Structure checks run
before projection; normal validation still checks the filesystem on resume and closure.

`update` merges only named project fields and task IDs. Lists replace their supplied field; objects
merge recursively; omitted tasks remain unchanged. New-task defaults remove mechanical boilerplate
without supplying success criteria, effects, or consent. The existing guarded transaction remains
responsible for revisions, immutable history, state transitions, dependencies, authorization,
output/evidence checks, and index rebuilding. Active executor ownership still refuses these updates.

These commands are stdlib Python behind the existing host launchers. An MCP service would add a
connection and deployment surface without removing more work from this local-file workflow, so this
change does not add one.

Optional briefing scaffolds require `init --briefing`. Missing report pairs no longer produce
ordinary closure warnings; explicit `--report` validation retains the paired-format contract.
The existing nonempty `reflection.md` closure requirement remains, satisfied by a short handoff.
Memory maintenance is optional, and a unique discovered workspace root no longer requires another
confirmation. Ambiguous roots still require a choice.

## Measured reduction and limits

- Entry instructions: 6,172 → 1,389 whitespace-delimited words, about 77% smaller.
- Real-project initial context: 6,683 bytes for `2026-08-28-002`, versus 84,802 bytes for its
  canonical JSON alone, about 92% smaller. The context includes a truncated specification and
  omits historical task detail; this is a smaller starting payload, not lossless compression.
- A regression fixture with 200 completed tasks keeps default context below 8.5 KB and checks that
  truncation, omitted-task counts, selected task details, and ready-task filtering stay explicit.

These are instruction/payload measurements, not an end-to-end agent speed benchmark. No live worker
run was commissioned. Full index rebuilding and the existing schema remain; this avoids changing
storage guarantees while removing agent bookkeeping. Reading a truncated specification's relevant
sections and verifying actual deliverables still costs work and must not be skipped.

Validation: full repository suite (1,526 passed, 3 skipped, 209 subtests), 100% shipped-script
statement coverage; repository-wide Ruff and ty; lockfile/version consistency; both host launchers;
Claude strict marketplace/plugin validation; project skill frontmatter validation. The generic
skill validator rejects grill's pre-existing Claude-only `user-invocable` key; the repository's
cross-host contract and tests explicitly retain that accepted host difference.

Release manifests and lockfile are aligned at `0.10.0`. Installed host caches are unchanged; a
marketplace update/reinstall is needed to activate the revised skill in a new session.

## Follow-up — 0.11.0, 2026-09-20

Reports now have independent Markdown and HTML checks through `--report-format markdown|html|both`.
Bare `--report` retains the legacy paired contract. A missing counterpart no longer produces a
closure warning; requested reports remain required task outputs. HTML instructions are loaded from
`report-html.md` only for HTML work, so Markdown reporting does not load CSS or chart geometry.

Substantial tasks now use fresh native agents sequentially when the host permits this. The
coordinator retains specification decisions, canonical writes, and recorded acceptance checks;
workers receive one task and return short results with artifact references. Trivial work stays inline,
as does work on hosts without fresh-context, observation, or stopping capabilities. The existing
parallel executor and v3/v4 state formats are unchanged.

`context --task T01 --worker` returns the complete task, direct dependency references, roots, revision,
and executor activity without reading specification text or evidence logs. The coordinator supplies
relevant constraints, decisions, inputs, and the assignment's relationship to the user's authorized
outcome. Requirements are not truncated. A regression fixture verifies that adding 200 unrelated
completed tasks leaves this projection exactly unchanged. This is a payload property, not a measured
reduction in total model tokens; worker startup and repeated discovery still have costs.

Native worker handles and launch intent are recorded in coordinator-owned task notes for recovery.
Unknown ownership blocks redispatch. These are workflow instructions, not the parallel executor's
durable process supervision, and `execution_active` does not track native agents.

Validation: 1,537 tests and 220 subtests passed, 3 skipped, 100% shipped-script statement coverage;
repository-wide Ruff and ty; lockfile/version consistency; both host launcher surfaces; Claude strict
marketplace/plugin validation; project skill frontmatter validation. Fresh Codex agents were used for
the actual implementation and a separate workflow trial. The temporary trial reached task planning
and a fresh worker launch, but automatic approval review rejected its fixture utility as outside the
authorized scope, including a retry with additional context. No fixture utility was written and no
successful end-to-end trial is claimed. The Claude live context-isolation check could not start because
the CLI was not logged in. Host guidance also references official subagent documentation; neither
documentation nor launcher tests substitute for those uncompleted live checks.

Release manifests and lockfile are aligned at `0.11.0`. Installed host caches are unchanged.

## Follow-up — 0.12.0, 2026-09-20

The user confirmed lean defaults with advanced features retained, existing formats preserved,
total-token priority with bounded coordinator context, selective delegation, and lifecycle trials.

The entrypoint now contains 792 words, down from 1,526 (48%). Entry instructions plus routine
commands total 1,282 words, down from 2,515 (49%). Including native worker instructions totals
1,703 words, down from 3,284 (48%). Counts use whitespace-delimited words, not model tokens.
Short memory and executor operation guides avoid loading design history for routine operations.
The full architecture, protocol, schema and HTML references remain available on demand.

New additive commands/projections:

- `context --task T01 --task-only` retains the complete assignment and direct dependency references
  without repeating the general resume view; legacy `--task` and `--worker` behavior is preserved.
- `read <project> spec --section <heading>` and `read <project> evidence --task T01` return bounded,
  paged source text. Fenced output cannot masquerade as a heading. Truncation and continuation are
  explicit; selected requirements and failed command results are never rewritten as summaries.
- `list-projects` filters/paginates discovery; `search-memory --limit` returns bounded excerpts.
  Legacy memory search remains available. Discovery identifies truncated metadata and unreadable
  records; it does not replace project validation.
- `--report-profile concise` makes task-graph accounting optional for requested reports. The
  execution profile and omitted-profile legacy commands retain strict checks. Ordinary closure
  no longer warns about omitted optional graphs. Required outputs and real evidence remain guarded.

Delegation now depends on investigation volume and useful independence. Small, related work can
reuse context. The coordinator retains ownership, canonical updates, and acceptance evidence;
worker recovery information remains durable. A valid recorded check can be reused for unchanged
outputs; a worker summary is still not command evidence.

### Representative lifecycle measurements

Reproduce with:

```sh
uv run pytest -q -s --no-cov -o log_cli=false tests/plugins/research/project/test_lifecycle_payloads.py
```

Each trial executes planning, three dependent tasks, real command verification, state updates,
and closure through CLI entrypoints. The resumed scenario includes a failed check, reloading
context, validation, evidence inspection, and correction. The fixtures use existing v3 state;
the independent native-agent trial below exercises new v4 initialization.

The baseline uses the preserved pre-change `context --task` projection and a full evidence read;
the lean trial uses task-only context and selected evidence. Both load the same current
specification at resume and finish with the same verified output and terminal task state.
Paths are normalized for comparison. The table measures emitted tool-result bytes, not total
conversation context, input/output model tokens, cache savings, or billed usage.

| Scenario | Results observed | Baseline bytes | Lean bytes | Reduction | Largest result, before → after |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small, three tasks | 14 | 19,408 | 9,697 | 50.0% | 5,117 → 3,820 |
| 200 completed tasks, three new tasks | 14 | 19,448 | 9,716 | 50.0% | 5,134 → 3,839 |
| 200 completed tasks, failure and simulated resume | 18 | 24,918 | 14,938 | 40.1% | 5,134 → 4,262 |

Counts of observed results remain equal before/after. The failure baseline includes one direct
evidence-file read in those observations. Regression tests enforce at least 20% less aggregate
payload on these fixtures, lossless requirement/evidence retrieval, and instruction-size budgets.
These fixtures isolate retrieval overhead; they do not model large-scale implementation work or
prove a universal reduction. Neither total model tokens nor peak agent context was exposed by
this session's tools, so both remain unmeasured.

A separate fresh native agent followed the revised skill in an isolated temporary workspace:
v4 initialization, planning, execution, recorded exact-byte verification, simulated resume, and
closure all passed in ten lifecycle CLI invocations. Its only warnings concerned the intentionally
unversioned fixture workspace. The coordinator independently checked its output and closure and
ran task-only, specification and discovery commands through the launcher under Python 3.9.6.
This is a current-workflow forward test, not a controlled before/after model-token experiment or
a real cross-session restart. No live Claude model trial was run; both launcher surfaces were
tested, including concise/strict report behavior.

Final validation: 1,560 tests and 220 subtests passed, 3 skipped; 100% shipped-script statement
coverage; repository-wide Ruff and ty; lock/version checks; both host launcher surfaces; strict
Claude marketplace/plugin validation; project skill validation. Versions are aligned at `0.12.0`.
Installed marketplace copies were not updated; they require a host update/reinstall.
