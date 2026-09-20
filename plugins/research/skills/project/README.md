# Project

A Claude Code and Codex skill for work that survives across sessions:

```text
/research:project Use /path/to/workspace. Implement the parser fix in /path/to/repo,
verify malformed-input handling, and leave the change ready for review.
```

Keep a concise specification, canonical tasks, real verification evidence, and a short handoff.
Ordinary one-turn changes usually do not need a persistent project. Clear requests proceed directly;
substantial uncertainty can use the internal grill interview.

The default lifecycle minimizes repeated context and bookkeeping. Investigation-heavy or independent
work can use fresh native agents; small, related work stays together when context reuse is cheaper.
Reports, graphs, memory promotion, interviews, separate review checkpoints, and parallel execution
remain available on demand. Total token consumption and peak context are separate measurements.

Use the supplied workspace, otherwise `RESEARCH_WORKSPACE`, otherwise discover established roots.
A unique root is used and stated; ambiguous or missing roots need a choice. New roots use the user's
requested location. Existing v3/v4 projects need no migration for ordinary work.

## Commands

Claude uses launchers on `PATH`; Codex resolves them beside the loaded skill.

```sh
research-project list-projects /path/to/workspace --query parser
research-project context /path/to/project
research-project context /path/to/project --task T01 --task-only
research-project read /path/to/project spec --section "Constraints and important assumptions"
research-project read /path/to/project evidence --task T01
research-project update /path/to/project /path/to/patch.json --expected-revision 2
research-project record-evidence /path/to/project --task T01 -- uv run pytest -q
research-validate /path/to/project --close --check-index
```

Resume context bounds active tasks and specification text. Task-only and worker views preserve the
complete task and direct dependency references without loading the specification or logs. Supply
applicable constraints before acting. Paged reads explicitly report truncation and the next offset;
follow pages until relevant requirements are complete. Validation still checks filesystem evidence.

Updates preserve revision, transition, dependency, authorization, output and evidence guards.
Only the coordinator writes canonical state. Destructive/external effects need current scoped
authorization; prior user authorization counts. External completion records a receipt.
Already completed task history remains immutable; maintenance appends new tasks.

Requested reports default to concise Markdown. Use `--report-format markdown --report-profile concise`
to check without task-graph accounting. Legacy report commands remain strict; execution reports,
HTML and paired formats remain available. Closure keeps a short `reflection.md`.

See [SKILL.md](SKILL.md) for the lifecycle and [commands.md](references/commands.md) for command
semantics. Specialized references are loaded only for their operation. Installed copies are managed
by host marketplaces: source edits require a host update/reinstall to take effect in a new session.
