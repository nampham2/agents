# agents

Cross-host research plugins for Claude Code and Codex.

## Install through a marketplace

Plugins are distributed through the repository marketplace for each host. Do not install a plugin
by symlinking or treating the repository root as a single plugin.

### Claude Code

Add the marketplace directly from GitHub; no clone is required:

```bash
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Confirm with `claude plugin list`. The plugin installs one user-facing skill,
`research:project`.

### Codex

Add the marketplace directly from GitHub; no clone is required:

```bash
codex plugin marketplace add nampham2/agents --ref main
codex plugin add research@agents
```

Confirm with `codex plugin list`.

## Development from a clone

```bash
git clone https://github.com/nampham2/agents.git
cd agents
uv sync --dev
```

For Claude Code, the helper validates and registers this clone as the local `agents` marketplace:

```bash
bin/install-plugin.sh research
```

For live Claude development without an installed cache:

```bash
claude --plugin-dir plugins/research
```

Claude permits only one source for a marketplace name. Switch a local `agents` marketplace back to
GitHub with:

```bash
claude plugin marketplace remove agents
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Removing a Claude marketplace also removes plugins installed from it.

## Releases

### 0.23.0 writing rules and advisory style findings

Project records now follow writing rules that ASD-STE100 inspired, and the validators give advice
when a living record does not. Nothing adds a field to `project.json`, so older launchers read
everything this release writes and simply give no advice.

- **Writing rules.** `references/writing-rules.md` holds 45 rules and a ten-term glossary. Thirteen
  rules come from the public list of STE rules and 32 are the project's own; each is marked. The
  file adds 774 words to every scenario that writes records. `references/writing-rules-extra.md`
  holds the preferred-word table and the STE rules the project does not apply, and is read only on
  demand. The project has no official copy of ASD-STE100, claims no compliance, and says so.
- **Advice, never a block.** For projects created on or after 2026-10-05T10:40:00Z, `research-validate`
  and `context --validate` report up to five findings per document and count the rest: sentences over
  25 words (20 for the task `verification` field and for numbered items in `handoff.md`), paragraphs
  over six sentences, preferred words, contractions, and `will`, `should` or `could`. The check reads
  the current specification, `architecture.md`, `handoff.md` and the task fields, in every status.
  It checks form only. About 40 of the 45 rules have no code check, because they need a parser.
- **Word budgets.** Each scenario budget rose by the 774 words. The alignment guard changed from
  10 percent below the 0.21.0 size to below it: alignment (repository) is 7,746 words against 7,813,
  a margin of 67 words.

### 0.22.0 one rule per transition, agreement tied to content, compact alignment

Fixes for eight findings of a review of 0.21.0, where the same transition was allowed or refused
depending on which command asked. Nothing adds a field to `project.json`, so a 0.21.0 launcher still
reads everything this release writes; it simply does not enforce the new rules. New checks judge only
the transition being committed: a task already `DONE` and a project already closed are never
re-judged.

- **Agreement is tied to content.** `workflow confirm` records a digest of the text the user agreed
  to beside its confirmation. Rewriting the specification or `architecture.md` afterwards makes the
  confirmation stale: the gate refuses `ALIGNING` to `PLANNING` and names the changed document, and
  `edit` and `round` return `stale_confirmations`. Decision history, the architecture link, the
  status word, the confirmation block and blank lines do not count as changes.
- **Finishing a task is one rule.** An `update` setting a task `DONE` is refused for a failed,
  foreign or ambiguous evidence record, as `task finish` always was. A reference the tool did not
  record cannot be judged: a warning for projects created before the cutoff, an error after it, and
  an observation-only finish must go through `task finish --observation`.
- **Closing a project is one rule.** `close` and an `update` to `DONE` refuse a recorded worktree
  with no keep or remove decision, as `finalize` did; `workflow readiness` and
  `research-validate --close` report the same finding once.
- **Worktree decisions can change.** `keep`, `accept_dirty` and `remove` may be recorded again on a
  kept worktree; the earlier decision is quoted into the decision history first, so the call needs
  the `spec` token. `accept_dirty` covers the paths dirty at that moment, and a new path is a closure
  finding. A removed worktree stays final.
- **Malformed worktree records** are findings, not a crash in validation.
- **Compact alignment.** `workflow confirm` accepts `kind: alignment`, one reply recorded against both
  document tokens, for a clear low-effort proposal. The full alignment reading path is 10.8 percent
  shorter (7,813 to 6,966 words) after removing duplicated procedure, and two pieces of guidance now
  load only when needed: `handoff-verifiers.md` and `memory-promotion.md`.
- **Tests.** The instruction budgets count every required read and a routing-drift test fails when a
  loaded reference links one that is neither counted nor classified; a complete current-project
  lifecycle runs through both launchers as subprocesses.

Compatibility: a project confirmed by 0.21.0 that has not yet left `ALIGNING` has no content digest;
0.22.0 refuses it with a message to confirm again. A project that already left `ALIGNING` is not
affected.

### 0.21.0 alignment gate and task stamps for new projects

Two changes that apply only to projects created on or after the release instant recorded in
`GATES_ENFORCED_FROM` (`workspace_lib.py`); every project that existed before it keeps the warnings
it always had, and no older launcher is locked out by the gate itself.

- **Alignment gate.** A new project may not leave `ALIGNING` (or `BLOCKED`) for `PLANNING` or
  `EXECUTING` until its seven specification sections are filled, `architecture.md` is agreed, and
  `workflow confirm` recorded both a requirements and an architecture confirmation. The refusal
  names each missing fact and the command that records it. Only that transition is gated; a reopen
  and a draft design revision mid-execution go through. It proves the tool's confirmation path ran,
  not that the user agreed.
- **Task stamps.** A new project's tasks carry `started_at` (first start; a restart after `BLOCKED`
  keeps it) and `finished_at`; `--backfill` sets `finished_at` only. The task graph prefers them to
  evidence stamps.

Compatibility: a launcher older than 0.21.0 cannot read a project whose tasks carry the stamps; it
reports the unexpected field and names a newer plugin as the likely cause. Update the plugin and
restart before resuming such a project.

### 0.20.0 validation agrees, observations, discoverability

- Standalone validation and the commit path now agree about finished work whose files moved: a
  finished task's missing required output or evidence file, and a finished project's missing working
  directory, are warnings on every path. 35 of 87 historical projects failed `research-validate` only
  for this; none do now, and no project that passed fails. A task becoming `DONE` still errors.
- A reopened project can record a new target worktree after the earlier one was removed, at a new
  path; only the latest target constrains the target root.
- `research-project record-observation` records a check with no command (an MCP read, a query) as an
  agent-attested entry, finished on only with `task finish --observation`; `--evidence` refuses it.
- `workflow <action> --schema` prints an action's allowed and required keys from the same table that
  validates it; input errors name the offending key and the action; `--help` names `research-project`.
- `list-projects` shows `created` and `updated` and takes `--older-than-days`.

Compatibility: nothing in this release adds a field an older launcher rejects. An older launcher sees
an observation entry as not selectable and refuses to finish on it.

### 0.19.0 recorder, handoff and launcher guards

Built from what 85 past projects actually tripped over:

- `record-evidence` refuses a relative path given to `research-validate` or `research-project`,
  exports `RESEARCH_PROJECT_DIR`, adds `--cwd` and `--dry-run`, and warns (without blocking) on
  evidence for a task that is not `RUNNING`, a heredoc or an unguarded pipe inside `bash -c`.
- `task finish --backfill --note` records work done before `task start` as two guarded commits.
- Handoffs gain a first-class `do_not` field, a stale-handoff banner, and a closure hint;
  `finalize` refuses a reflection that is not a post-mortem; a kept worktree can be recorded as
  removed.
- Every command warns on stderr when a cached, out-of-date plugin copy is running.
- `research-project stage` writes a triage-ready staged lesson; `promote-memory` is idempotent.

Compatibility: a handoff written with `do_not` cannot be read by an older launcher. Update the
plugin, restart Claude Code, and then resume projects.

### 0.18.1 effort scale

Effort in architecture proposals is rated low, medium or high per workstream and overall, anchored
on scope signals, and never estimated in hours or days. External waits are named as dependencies.

### 0.18.0 git worktree discipline

Every repository a project writes to gets a user-confirmed, recorded git worktree before the first
write (`workflow worktree`, `record` and `close`), and closure needs the user's commit and
keep or remove decision for each one. `project.json` gains an optional `worktrees` list; older
launchers cannot read a project that records one.

### 0.17.1 checkable handoffs

Handoffs follow the provenance, verifier-status and preservation rules in `handoff-writing.md`:
a claim the successor will rely on carries its source and observation time, and one that was not
checked is marked `UNVERIFIED` with the re-check.

### 0.17.0 execution change

Projects now use normal task execution and scoped native subagents. The automatic executor and
`run-auto`, `run-once`, `run-parallel`, and `enable-execution` commands are removed. New projects
create no `execution/` store or empty `tasks/`, `artifacts/`, or `reviews/` directories.
Existing idle v3/v4 projects remain supported without migration; historical files are untouched.
Resolve active legacy executor ownership with the previous compatible installation before upgrading.
This version preserves the ownership guard but cannot perform legacy executor recovery.

### Versioning and updates

`pyproject.toml` owns the release version. `uv.lock` and the Claude Code and Codex plugin
manifests must use exactly the same SemVer; CI enforces this contract.

Claude Code caches installed plugins by version. After publishing a version bump:

```bash
claude plugin marketplace update agents
claude plugin update research@agents
```

Restart Claude Code to apply the update.

## Checks

```bash
uv run pytest -q
uv run ruff check .
uv run ty check .
uv lock --check
```
