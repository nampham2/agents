# Project

A Claude Code and Codex skill for work that survives across sessions:

```text
/research:project Use /path/to/workspace. Implement the parser fix in /path/to/repo,
verify malformed-input handling, and leave the change ready for review.
```

Keep a concise specification, canonical tasks, real verification evidence, and a short handoff.
Ordinary one-turn changes usually do not need a persistent project. Within a project, requirements
grill and planning review are mandatory before task planning. Clear requirements can use zero
question rounds, followed by a shared summary and explicit confirmation.

The grill is a procedure of this skill, documented in [grill.md](references/grill.md), not a
separate command. It interviews the user in rounds over a tree of decisions, asking each round's
whole frontier with a recommended answer per question, looks up facts itself, and ends with an
explicit confirmation. It writes the agreed specification and dated decisions into the project
workspace and never starts work on the strength of the interview alone.

The alignment loop is iterative: grill requirements, review the plan and effort, revisit
affected requirements, and repeat until agent and user agree. The required `planning.md` records
the approach, work breakdown, effort levels with assumptions, edge cases, verification and risks,
with an editable dependency diagram. Only then are implementation tasks planned. On resume, reuse
current agreement; changes to requirements or the plan reopen the affected review. See
[planning-review.md](references/planning-review.md) for the document and agreement contract.

A project that writes code starts with a design task. It writes `artifacts/architecture.md`, the
living technical design with diagrams, contracts, algorithms, failure modes and an implementation
status row per task, and each code task keeps it current. A Goldfish round reviews it before the
first code task; see [design-document.md](references/design-document.md). A project begun before
the split keeps its agreement in a root `architecture.md`, which the tools read as the planning
document. A plugin older than 0.30.0 fails on a project that has the new `planning` document id;
refresh the plugin.

The lifecycle minimizes repeated context and bookkeeping. Investigation-heavy or independent
work can use fresh native agents; small, related work stays together when context reuse is cheaper.
Reports, task graphs, memory promotion and separate delivery review checkpoints remain available
on demand. The automatic executor is retired; native workers use the host's agent tools.
Total token consumption and peak context are separate measurements.

Phase boundaries checkpoint progress and continue in the same session. Fresh sessions are for user
requests or context pressure that impairs continuation. Routine corrections within the current
assignment use task findings and affected checks; material changes reconcile dependencies and
agreement. Starting a task from `PLANNING`, `BLOCKED` or `REVIEW` moves the project to `EXECUTING`,
preserving review state and normal guards. Reopen invalidated delivery acceptance before closure.

Project status and task definitions live in `project.json`; `tasks/<id>.md` holds optional findings
and worker-recovery notes. `friction.md` holds what the skill or the tool got in the way of, and
`invocations.jsonl` every project-scoped command the tool ran; `research:retro` reads both.
`artifacts/` holds durable supporting files and requested deliverables.
`reviews/review_NN.md` records explicit delivery checkpoints, not the initial planning review.
These directories appear only when used. Design changes update `artifacts/architecture.md`;
implementation findings and verification do not need copying there. See
[durable-context.md](references/durable-context.md#keep-the-layout-small-and-consistent).

Use the supplied workspace, otherwise `RESEARCH_WORKSPACE`, otherwise discover established roots.
A unique root is used and stated; ambiguous or missing roots need a choice. New roots use the user's
requested location. Existing v3/v4 projects need no schema migration; missing planning records
and agreement are completed before new planning or affected implementation. Validation warns when a
project in `PLANNING`, `EXECUTING`, or `REVIEW` lacks an agreed `planning.md`; it cannot verify
conversational agreement, and legacy projects stay valid. A project created on or after the gate
cutoff is refused the move out of `ALIGNING` until its specification is filled, its planning
document is agreed and `workflow confirm` recorded both confirmations; its tasks also carry
`started_at` and `finished_at`, which an older launcher cannot read.

A repository target needs a user-confirmed, recorded worktree before the first write; see
[worktrees.md](references/worktrees.md). A removed target does not stop a reopened project from
recording a new one, at a new path. An optional briefing (`init --briefing`) can seed the grill; it
is checked only when `briefing.md` exists and is never required.

## Duplicate guard

`init` compares the new title with every project in the workspace before it creates a directory. It
scores word overlap with a weighted Jaccard similarity (words that many titles share count for
little). A likely duplicate is a similarity of at least 0.35 on the same repository (a
`<repo>.worktrees/<id>` path counts as `<repo>`) or at least 0.5 elsewhere. A live project
(`ALIGNING`, `PLANNING`, `EXECUTING`, `REVIEW`, `BLOCKED`) that matches blocks `init`: nothing is
created and the refusal lists each candidate. Show the candidates to the user. Only after the user
chooses a new project, rerun `init --acknowledge-duplicates <project-id>...`; the spec history then
records the ids. A finished project (`DONE`, `CANCELLED`) that matches is listed as a warning and
never blocks. `find-similar <root> --title "<title>" --working-directory <dir>` prints both lists
without creating anything. The thresholds come from the calibration of the project that added the
guard; recalibrate when the workspace grows a lot.

`list-projects --query` matches a project when every word of the query occurs in its row, so a
multi-word objective finds the project; one literal phrase still matches as before.

## Commands

Claude uses launchers on `PATH`; Codex resolves them beside the loaded skill.

```sh
research-project list-projects /path/to/workspace --query parser
research-project find-similar /path/to/workspace --title "New title" --working-directory /path/to/target
research-project list-projects /path/to/workspace --status ALIGNING --older-than-days 20
research-project workflow /path/to/project resume -            # JSON {} on stdin
research-project workflow /path/to/project resume --schema     # allowed and required keys
research-project context /path/to/project --validate
research-project read /path/to/project spec --outline
research-project edit /path/to/project spec --sections-json - --expected-sha256 TOKEN
research-project update /path/to/project - --expected-revision 2 --json
research-project task /path/to/project start T01 --expected-revision 3
research-project record-evidence /path/to/project --task T01 --json -- uv run pytest -q
research-project record-evidence /path/to/project --task T01 --dry-run -- uv run pytest -q
research-project record-observation /path/to/project --task T01 --source "query" \
  --result passed --body-file -
research-project task /path/to/project finish T01 --evidence RECORD_ID --expected-revision 4
research-project task /path/to/project finish T01 --observation RECORD_ID --expected-revision 4
research-project task /path/to/project finish T01 --backfill --note "why" --evidence RECORD_ID \
  --expected-revision 4
research-project stage /path/to/project --title "One line" --body-file -
research-project workflow /path/to/project finalize -          # closes the project
```

`close` is the older closing command and remains for existing scripts; `workflow finalize` also
saves the final handoff. Every command warns on stderr when a cached, out-of-date plugin copy is
the one running.

Four commands are rarely needed. `commit` applies a full candidate state through the guards, and
`commit --dry-run` reports every problem it would hit. `migrate` previews a schema migration unless
told to apply it. `rebuild-index` regenerates `INDEX.md` after a reported index failure.
`show-graph` prints a project's task graph.

Resume context bounds active tasks and specification text. Task-only and worker views preserve the
complete task and direct dependency references without loading the specification or logs. Supply
applicable constraints before acting. Paged reads explicitly report truncation and the next offset;
follow pages until relevant requirements are complete. Validation still checks filesystem evidence.

Evidence that no command can produce, such as an MCP read or a query, is recorded with
`record-observation`. The entry is labelled as the agent's own account, carries the verdict you
supply and no exit code, and is finished on only with `task finish --observation`; `--evidence`
refuses it. That makes it weaker than a recorded exit code, and an observation-only finish leaves a
note saying so.

Guarded Markdown edits use document hashes to reject stale writes. Decisions and task findings
append without custom rewrite scripts. Updates preserve revision, transition, dependency,
authorization, output and evidence guards. Only the coordinator writes canonical state.
Destructive/external effects need current scoped authorization; prior user authorization counts.
External completion records a receipt. Already completed task history remains immutable; maintenance
appends new tasks.

Requested reports default to concise Markdown. Use `--report-format markdown --report-profile
concise` to check without task-graph accounting. Legacy report commands remain strict; execution
reports, HTML and paired formats remain available. Closure keeps a short `reflection.md`.

Agents write records to the project's own [writing rules](references/writing-rules.md), which
ASD-STE100 inspired: 45 rules and a short glossary. For projects created after the style cutoff,
`research-validate` and `context --validate` add advice for the living records: long sentences and
paragraphs, preferred words, contractions, and `will`, `should` or `could`. The advice never blocks
a command and checks form only. The [word table](references/writing-rules-extra.md) lists the
preferred words and the STE rules that the project does not apply.

See [SKILL.md](SKILL.md) for the lifecycle and [commands.md](references/commands.md) for command
semantics. Specialized references are loaded only for their operation. Installed copies are managed
by host marketplaces: source edits require a host update/reinstall to take effect in a new session.
