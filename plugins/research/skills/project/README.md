# Project

A Claude Code and Codex skill for work that needs to survive across sessions. Invoke
`/research:project` with the outcome and workspace location, or set `RESEARCH_WORKSPACE` once.

```text
/research:project Use /path/to/workspace. Implement the parser fix in /path/to/repo,
verify malformed-input handling, and leave the change ready for review.
```

The skill keeps a short specification, canonical task state, and verification evidence. It asks
about unresolved decisions and proceeds when your request is clear. Ordinary one-turn changes
usually do not need a persistent project.

## Defaults

- Sequential work, with a few tasks representing deliverable milestones.
- Compact resume context instead of loading all task history, logs, and memory.
- Small revision-checked updates; code maintains the full JSON state.
- Real command evidence captured by the tool.
- A brief closure/handoff note. Reports, charts, task-graph presentations, memory promotion,
  alignment interviews, and parallel workers are optional.

Use the supplied workspace path, otherwise `RESEARCH_WORKSPACE`. Without either, the skill
searches established roots, uses a unique result, and asks if discovery is ambiguous or empty.
A new root is created only at the location you request. The skill states the root before writing.

## Start, resume, review

```text
/research:project Start a project to produce an onboarding guide in docs/onboarding.md.
Audience: new analysts. Verify every command.

/research:project Resume project 2026-09-18-001 and implement the next task.

/research:project Record this feedback and revise the guide: <feedback>

/research:project Validate the project and close it when the required work is complete.
```

Clear feedback updates the specification directly. Substantial ambiguity can use the internal
`grill` interview. Review checkpoints are required when requested, not for every project.
Maintenance reopens the project that owns the deliverable and appends new tasks; completed
history remains immutable.

Repository outputs stay in the target repository. Workspace notes remain in the project directory.
Destructive and external actions need authorization for the exact scope; already authorized work
does not need repeated confirmation. Completed external work also records a durable receipt.
A requested code change does not automatically add publishing, pushing, or merging.

## Commands

Both hosts use the same stdlib Python implementation. Claude invokes the commands from `PATH`;
Codex uses launchers beside the loaded skill.

```sh
research-project init /path/to/workspace --title "Parser fix" --working-directory /path/to/repo
research-project context /path/to/project
research-project context /path/to/project --task T01
research-project update /path/to/project /path/to/patch.json --expected-revision 2
research-project record-evidence /path/to/project --task T01 -- uv run pytest -q
research-validate /path/to/project --close --check-index
```

See [references/commands.md](references/commands.md) for patch examples and optional executor use.
`update` uses the existing commit guards and automatically derives `current_tasks`; omitted tasks
and fields remain unchanged. It does not bypass dependencies, evidence, authorization, or revision
checks. A stale revision requires reloading and reconciling.

`context` checks structure and returns task counts, up to five active summaries/ready IDs, the
current specification (up to 6,000 characters), and review/execution state. Truncation is explicit.
`--task` returns one task in full. Run the validator on resume to check filesystem evidence too.

New projects remain schema v4 for compatibility, but initialization does not dispatch workers.
`init --briefing` adds the optional discovery template. Older v3 projects remain sequential;
migration and execution activation still require explicit authorization.

## Optional extras

Use `show-graph` when dependencies are worth visualizing. Choose `run-auto` only when workers
are authorized and its narrow admission rules fit the work; it is not a prerequisite for sequential
execution. Existing executor state and recovery guarantees remain supported.

Write a report when it is part of the deliverable, in the format the user needs. The historical
Markdown/HTML pair and `research-validate --report` remain available; no report files means no
report warning at closure. A short `reflection.md` remains required by the existing schema.

Cross-project memory is searched on demand. Promoting a useful lesson is optional; every commit
still regenerates indexes. If index generation fails after a commit, run the recovery command it
prints instead of repeating the mutation.

Installed plugins are copied into host-managed caches. Updating this source tree does not change
an installed copy; update/reinstall through the host marketplace to use the new release.

See [SKILL.md](SKILL.md) for the workflow and
[references/workspace-schema.md](references/workspace-schema.md) for the stored format.
