# AGENTS.md

Instructions for Claude and other AI agents working in this repository.

## Repository purpose

Personal toolkit of Claude agent skills, plugins, and supporting Python utilities.

## Structure

```
plugins/    Claude plugins; each subdirectory is one installable plugin
src/agents/ Shared Python library (importable package)
tests/      pytest test suite mirroring the plugins/ layout
bin/        Helper shell scripts
```

## Development

```bash
uv sync --dev          # install all dependencies
uv run pytest          # run tests
uv run ruff check .    # lint
uv run ty check        # type-check
```

## Conventions

- Python ≥ 3.11, type hints on all public functions
- ruff line-length 120, same lint rules as metasearch-ai
- Tests live under `tests/plugins/<plugin>/<skill>/`
- Plugin skills live under `plugins/<plugin>/skills/<skill>/`
