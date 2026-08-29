# agents

Personal toolkit of Claude agent skills and plugins.

## Install a plugin

```bash
bin/install-plugin.sh research
```

Claude Code registers plugins through a **marketplace**, not by directory placement. This repo
root is a directory marketplace (`.claude-plugin/marketplace.json`), so the script validates the
manifests, registers the marketplace, and installs the plugin from it. Confirm with:

```bash
claude plugin list
```

Symlinking a plugin into `~/.claude/plugins/` does *not* register anything — `claude plugin list`
still reports `No plugins installed`.

Skills are discovered from `skills/<name>/SKILL.md` and addressed as `<plugin>:<skill>` — for
example `research:workbench`.

## Working on an installed plugin

Installing **copies** the plugin into a version-keyed cache
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`). Editing this working tree does not
change that copy, and `claude plugin marketplace update` does not either: it refreshes the
marketplace listing only, reports success, and leaves the cached plugin exactly as it was. With
`version` unchanged, `claude plugin update` then reports the installed version as already current.

For live development, load the plugin straight from disk instead — no install, no cache:

```bash
claude --plugin-dir plugins/research
```

To push a change into an *installed* copy, bump `version` in
`plugins/<plugin>/.claude-plugin/plugin.json`, then:

```bash
claude plugin marketplace update agents
claude plugin update research@agents      # restart Claude Code to apply
```

The version bump is what invalidates the cache; without it the update is a no-op.

## Development

```bash
uv sync --dev          # install deps
uv run pytest          # tests (100% coverage of the shipped scripts)
uv run ruff check .    # lint
uv run ty check .      # type-check
```
