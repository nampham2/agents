# agents

Cross-host research skills and plugin tooling for Claude Code, Codex, and Kimi Code.

## Install directly from GitHub (recommended)

You do not need to clone this repository. Install the `research` plugin from GitHub using the
instructions for your agent host.

### Claude Code

```bash
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Confirm the installation with `claude plugin list`. Skills are addressed as
`research:project` and `research:grill`.

### Codex

```bash
codex plugin marketplace add nampham2/agents --ref main
codex plugin add research@agents
```

Confirm the installation with `codex plugin list`.

### Kimi Code

Run these commands inside Kimi Code:

```text
/plugins install https://github.com/nampham2/agents/tree/main
/reload
```

The repository-root `kimi.plugin.json` exposes the research skills when Kimi installs directly
from GitHub. Run `/plugins` and check the Installed tab to confirm the installation.

These commands install the default branch, not a local working tree. Each host may cache or copy
the installed plugin, so update it through that host after a new release rather than editing its
managed files.

## Development from a clone

Clone the repository when you want to change or test the plugin itself:

```bash
git clone https://github.com/nampham2/agents.git
cd agents
uv sync --dev
```

For Claude Code, the repository helper validates both manifests, registers this clone as a local
directory marketplace, and installs the plugin:

```bash
bin/install-plugin.sh research
```

For live Claude development without an installed cache, load the plugin straight from disk:

```bash
claude --plugin-dir plugins/research
```

The marketplace name is `agents`. Claude Code allows only one source for a marketplace name, so a
local directory marketplace and the GitHub marketplace cannot be active at the same time. Switch
back to GitHub with:

```bash
claude plugin marketplace remove agents
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Removing a Claude marketplace also removes plugins installed from it. Symlinking a plugin into
`~/.claude/plugins/` does not register it.

## Releasing a plugin change

`pyproject.toml` is the source of truth for the release version. The lockfile and the Claude,
Codex, and Kimi manifests must all use exactly that version. CI enforces this rule.

Claude Code stores installed plugins in a version-keyed cache. After publishing a version bump,
refresh an existing Claude installation with:

```bash
claude plugin marketplace update agents
claude plugin update research@agents
```

Restart Claude Code to apply the update.

## Checks

```bash
uv run pytest
uv run ruff check .
uv run ty check .
uv lock --check
```
