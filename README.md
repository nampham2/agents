# agents

Personal toolkit of Claude agent skills, plugins, and supporting Python utilities.

## Install a plugin

```bash
bin/install-plugin.sh research
```

This symlinks `plugins/research` into `~/.claude/plugins/` so Claude Code picks it up.

## Development

```bash
uv sync --dev   # install deps
uv run pytest   # run tests
```
