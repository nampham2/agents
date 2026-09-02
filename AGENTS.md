# AGENTS.md

Instructions for Claude Code, Codex, Kimi Code, and other AI agents working in this repository.

## Repository purpose

Personal toolkit of cross-host agent skills, plugins, and supporting Python utilities.

## Structure

```
.claude-plugin/  marketplace.json — this repo root is a Claude Code directory marketplace
plugins/         Cross-host plugins; each subdirectory is one installable plugin
tests/           pytest test suite mirroring the plugins/ layout
bin/             Helper shell scripts
```

Plugin code is the only source of truth: there is no importable `agents` package and
nothing to build. Skill scripts live beside their skill (e.g.
`plugins/research/skills/project/scripts/`) and are imported by tests as top-level
modules via `tests/conftest.py`, the same way Claude runs them.

## Development

```bash
uv sync --dev          # install all dependencies
uv run pytest          # run tests
uv run pytest -q       # the suite enforces 100% coverage of the shipped scripts
uv run ruff check .    # lint (whole repo — keep it unscoped, see below)
uv run ty check .      # type-check (whole repo, tests included)
```

## Conventions

- **Two Python floors.** The dev toolchain requires ≥ 3.11 (`requires-python` in
  `pyproject.toml`). The *shipped plugin scripts* must additionally keep working on
  **3.9**, because the `bin/` wrappers `exec python3`, which is 3.9.6 on stock macOS. Keep
  them stdlib-only, and put 3.10+ typing imports behind `if TYPE_CHECKING:` so
  `from __future__ import annotations` defers them. CI's `python39-compat` job enforces this.
- Lint and type-check the whole repo, tests included — never a subdirectory. Scoping
  these narrowly is what previously let two copies of the workspace library drift apart
  unnoticed. Both `ruff check .` and `ty check .` are clean; keep them that way rather
  than adding ignores.
- Tests resolve the plugin scripts as top-level modules. Two places encode that: the
  `sys.path` insert in `tests/conftest.py` (runtime) and `[tool.ty.environment]
  extra-paths` in `pyproject.toml` (type checker and editors). Update both together.
- Project state is `dict[str, Any]`, not `dict[str, object]` — test helpers that build
  state must match, or the nested subscripts they do will not type-check.
- **Plugins are registered through the marketplace, never by directory placement.** A symlink
  into `~/.claude/plugins/` registers nothing. `.claude-plugin/marketplace.json` publishes each
  plugin; `bin/install-plugin.sh` validates both manifests and installs through the `claude` CLI.
  A new plugin must be added to the marketplace or it is uninstallable.
- **`plugin.json` has no `skills` key.** Skills are discovered by convention from
  `skills/<name>/SKILL.md`; declaring them makes `claude plugin validate` fail outright. Run
  `claude plugin validate --strict .` and `--strict plugins/<plugin>` after touching a manifest —
  the installer does this for you, and CI cannot (no `claude` CLI there).
- **Migration must never manufacture consent.** Legacy authorization markers are coarse and
  project-wide; v3 authorization is per-action. Only a `DONE` task carries its marker forward:
  `SKILL.md` sets a task `RUNNING` *before* performing the action, so `RUNNING` does not prove the
  effect happened. A `RUNNING` external task migrates to `BLOCKED` with a `block_reason` and
  `pending` authorization — parked for reconciliation, because leaving it `RUNNING` with `pending`
  would fail v3 validation and make a valid v2 project unmigratable.
- **Validation code must not raise on malformed input.** Checks that run before
  `validate_v3_state` see arbitrary JSON, so they must degrade to "nothing to say" rather than
  throw; the CLIs only translate `WorkspaceError`. Read text through `read_text()`, which folds
  `UnicodeDecodeError` (a `ValueError`, not an `OSError`) into `WorkspaceError`.
- **`project.json` is the commit point.** Write it last in any multi-step mutation — `init` and
  `migrate` both build the skeleton first — so an interrupted run leaves a state a plain re-run can
  finish. The index rebuild that follows a commit takes a *workspace*-wide lock and can fail on its
  own; route it through `_rebuild_index_after_commit` so the error says the write landed and names
  `rebuild-index`, instead of reading as a failed commit and sending the caller into a retry that
  reports a conflict or an impasse. `rebuild_index` normalizes `OSError` into `WorkspaceError`
  for the same reason: a read-only workspace fails in the lock's own `mkdir`, and a raw
  traceback out of a CLI that only translates `WorkspaceError` is what makes a committed write
  look unfinished.
- **Plugin changes must work in Claude Code, Codex, and Kimi Code.** Keep each host's manifest and
  supported command-resolution surface valid; do not infer that behavior proven in one host carries
  to the others. Claude Code discovers skills by convention from `.claude-plugin/plugin.json`,
  Codex uses `.codex-plugin/plugin.json`, and Kimi Code uses `.kimi-plugin/plugin.json` and copies
  local installations into `$KIMI_CODE_HOME/plugins/managed/<plugin-id>/`. Test changed shared
  skills and scripts through every affected host surface.
- **The plugin has three host entry surfaces over one launcher implementation.** Claude Code puts
  `plugins/<plugin>/bin/` on `PATH`, so it invokes `research-project` and `research-validate` by
  name. Codex and Kimi Code do not add that directory to `PATH`; both invoke the same-named
  launchers resolved relative to the exact loaded `skills/project/SKILL.md`. Kimi's managed copy
  must preserve their executable modes. The top-level wrappers delegate to those skill-local
  launchers. Never search caches, infer a plugin root from the current repository, or invoke
  `manage_workspace.py` / `validate_workspace.py` directly from skill instructions. All launcher
  routes resolve through symlinks from `BASH_SOURCE[0]` and fail clearly when their script or
  `python3` is missing; `tests/plugins/research/project/test_bin_wrappers.py` covers the shared
  launcher behavior.
- **`CLAUDE_PLUGIN_ROOT` is not set in the Bash tool environment.** It is available to hooks and MCP
  server commands, not to commands a session runs, so nothing in a skill may depend on it. This was
  verified, not assumed — it is why the `bin/`-on-`PATH` surface exists rather than a
  `$CLAUDE_PLUGIN_ROOT/scripts/...` convention.
- **Task evidence is recorded by running the command, never by describing it.**
  `research-project record-evidence <project-dir> --task <id> -- <command>` runs the command with no
  shell and appends its real exit code and output tail to `evidence.md`; it exits non-zero and
  refuses to call a failure a pass. Hand-written prose belongs in a separate `### <id> — notes`
  section below the recorded entry, never inside it.
- **The coverage gate is 100%.** `[tool.coverage.report] fail_under = 100` over
  `plugins/research/skills/project/scripts`, so a new branch arrives with its test or the suite
  fails. Delete unreachable code rather than excluding it — an `OSError` guard around a call that
  cannot raise was the first thing this gate found.
- **Installed plugins are cached by version.** `claude plugin install` copies the plugin into
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`; `marketplace update` refreshes the
  listing, never that copy. Any plugin change that must reach an installed copy needs a `version`
  bump in `plugin.json` followed by `claude plugin update`. Use `claude --plugin-dir
  plugins/<plugin>` for live development. Do not drop `version` to get commit-based versioning —
  `claude plugin validate --strict` fails without it.
- Type hints on all public functions
- ruff line-length 120, same lint rules as metasearch-ai
- Tests live under `tests/plugins/<plugin>/<skill>/`
- Plugin skills live under `plugins/<plugin>/skills/<skill>/`
