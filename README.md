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
