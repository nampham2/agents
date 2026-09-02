# agents

Cross-host research plugins for Claude Code, Codex, and Kimi Code.

## Install through a marketplace

Plugins are distributed through the repository marketplace for each host. Do not install a plugin
by symlinking or treating the repository root as a single plugin.

### Claude Code

Add the marketplace directly from GitHub; no clone is required:

```bash
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Confirm with `claude plugin list`. Skills are named `research:project` and `research:grill`.

### Codex

Add the marketplace directly from GitHub; no clone is required:

```bash
codex plugin marketplace add nampham2/agents --ref main
codex plugin add research@agents
```

Confirm with `codex plugin list`.

### Kimi Code

Kimi installs `research` from `.kimi-plugin/marketplace.json`, which points to the nested plugin at
`plugins/research`. From a checkout, run this inside Kimi Code:

```text
/plugins marketplace /absolute/path/to/agents/.kimi-plugin/marketplace.json
```

Choose `research` in the marketplace, then run `/reload` or `/new`. Confirm with
`/plugins info research`.

Kimi's marketplace currently references the plugin by repository-relative path, so it needs a local
checkout. The Claude Code and Codex marketplace routes above remain the recommended no-clone GitHub
installation paths.

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

`pyproject.toml` owns the release version. `uv.lock` and the Claude Code, Codex, and Kimi Code plugin
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
