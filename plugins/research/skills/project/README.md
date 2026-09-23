# Project

A Claude Code and Codex skill for work that survives across sessions:

```text
/research:project Use /path/to/workspace. Implement the parser fix in /path/to/repo,
verify malformed-input handling, and leave the change ready for review.
```

Keep a concise specification, canonical tasks, real verification evidence, and a short handoff.
Ordinary one-turn changes usually do not need a persistent project. Within a project, requirements
grill and architecture review are mandatory before task planning. Clear requirements can use zero
question rounds, followed by a shared summary and explicit confirmation.

The grill is a procedure of this skill, documented in [grill.md](references/grill.md), not a
separate command. It interviews the user in rounds over a tree of decisions, asking each round's
whole frontier with a recommended answer per question, looks up facts itself, and ends with an
explicit confirmation. It writes the agreed specification and dated decisions into the project
workspace and never starts work on the strength of the interview alone.

The alignment loop is iterative: grill requirements, review architecture and effort, revisit
affected requirements, and repeat until agent and user agree. The required `architecture.md` records
modules, code organization, interfaces and flows, edge cases, trade-offs, verification, and effort
ranges with assumptions. Reviews prioritize editable diagrams: module relationships, key flows, a
directory tree, and additional sequence or state diagrams where useful. Only then are implementation
tasks planned. On resume, reuse current agreement; changes to requirements or design reopen the
affected review. See [architecture-review.md](references/architecture-review.md) for the document
and agreement contract.

The lifecycle minimizes repeated context and bookkeeping. Investigation-heavy or independent
work can use fresh native agents; small, related work stays together when context reuse is cheaper.
Reports, task graphs, memory promotion, separate delivery review checkpoints, and parallel execution
remain available on demand. Total token consumption and peak context are separate measurements.

Use the supplied workspace, otherwise `RESEARCH_WORKSPACE`, otherwise discover established roots.
A unique root is used and stated; ambiguous or missing roots need a choice. New roots use the user's
requested location. Existing v3/v4 projects need no schema migration; missing architecture records
and agreement are completed before new planning or affected implementation. Validation warns when a
project in `PLANNING`, `EXECUTING`, or `REVIEW` lacks an agreed `architecture.md`; it cannot verify
conversational agreement, and legacy projects stay valid.

## Commands

Claude uses launchers on `PATH`; Codex resolves them beside the loaded skill.

```sh
research-project list-projects /path/to/workspace --query parser
research-project context /path/to/project --validate
research-project read /path/to/project spec --outline
research-project edit /path/to/project spec --sections-json - --expected-sha256 TOKEN
research-project update /path/to/project - --expected-revision 2 --json
research-project task /path/to/project start T01 --expected-revision 3
research-project record-evidence /path/to/project --task T01 --json -- uv run pytest -q
research-project task /path/to/project finish T01 --evidence RECORD_ID --expected-revision 4
research-project close /path/to/project --expected-revision 5 --reflection-file - \
  --expected-reflection-sha256 missing
```

Resume context bounds active tasks and specification text. Task-only and worker views preserve the
complete task and direct dependency references without loading the specification or logs. Supply
applicable constraints before acting. Paged reads explicitly report truncation and the next offset;
follow pages until relevant requirements are complete. Validation still checks filesystem evidence.

Guarded Markdown edits use document hashes to reject stale writes. Decisions and task findings
append without custom rewrite scripts. Updates preserve revision, transition, dependency,
authorization, output and evidence guards. Only the coordinator writes canonical state.
Destructive/external effects need current scoped authorization; prior user authorization counts.
External completion records a receipt. Already completed task history remains immutable; maintenance
appends new tasks.

Requested reports default to concise Markdown. Use `--report-format markdown --report-profile
concise` to check without task-graph accounting. Legacy report commands remain strict; execution
reports, HTML and paired formats remain available. Closure keeps a short `reflection.md`.

See [SKILL.md](SKILL.md) for the lifecycle and [commands.md](references/commands.md) for command
semantics. Specialized references are loaded only for their operation. Installed copies are managed
by host marketplaces: source edits require a host update/reinstall to take effect in a new session.
