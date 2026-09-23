# AGENTS.md

Instructions for Claude Code, Codex, and other agents working in this repository.

## Repository

This repository ships cross-host plugins and stdlib-only Python utilities. Plugin files are the
source of truth; there is no importable `agents` package or build step.

```text
.claude-plugin/  Claude Code marketplace
.agents/plugins/ Codex marketplace
plugins/         Shared plugin implementations
tests/           pytest suite mirroring plugins/
bin/             Repository helpers
```

## Development

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
uv run ty check .
uv lock --check
```

Run lint and type checks over the whole repository, tests included. The test suite enforces 100%
coverage of `plugins/research/skills/project/scripts`.

## Cross-host plugin contract

Plugins are installed through each host's marketplace, never by symlink or direct directory
placement. Keep these surfaces aligned:

| Host | Repository catalog | Plugin manifest | Command entry |
| --- | --- | --- | --- |
| Claude Code | `.claude-plugin/marketplace.json` | `plugins/research/.claude-plugin/plugin.json` | `plugins/research/bin/` on `PATH` |
| Codex | `.agents/plugins/marketplace.json` | `plugins/research/.codex-plugin/plugin.json` | launcher beside the loaded project skill |

- A shared plugin change must work in Claude Code and Codex. Test every affected host
  surface; success in one host does not imply compatibility with the others.
- Claude invokes `research-project` and `research-validate` by name. Codex resolves the same
  executable launchers relative to the loaded `skills/project/SKILL.md`. Top-level wrappers
  delegate to those skill-local launchers.
- Never search plugin caches, infer a plugin root from the current directory, depend on
  `CLAUDE_PLUGIN_ROOT` in a session shell, or invoke `manage_workspace.py` and
  `validate_workspace.py` directly from skill instructions.
- Claude discovers `skills/<name>/SKILL.md` by convention; its `plugin.json` must not contain a
  `skills` key. After manifest changes, run `claude plugin validate --strict .` and
  `claude plugin validate --strict plugins/research`.
- Skill frontmatter is not portable. Claude Code honours `user-invocable: false`, which hides a
  skill's slash command while leaving it reachable through the Skill tool; Codex parses only `name`,
  `description`, and `disable-model-invocation`, so a skill hidden that way still appears as a Codex
  slash command. Plugin-internal procedures therefore live under `skills/<skill>/references/` and are
  loaded by the owning skill on demand, never as hidden sibling skills. The requirements grill
  (`skills/project/references/grill.md`) is the precedent. Never reach for `disable-model-invocation`
  to hide a skill: it is the opposite lever, and Codex rejects any value but `false`.
- `pyproject.toml` `[project].version` is canonical. It must exactly match the `agents` package in
  `uv.lock` and the Claude and Codex plugin manifests. Committed manifests use plain SemVer,
  without a Codex development cachebuster. Run `uv lock` after a bump and the focused CI check:
  `uv run pytest -q --no-cov tests/plugins/research/test_plugin_versions.py`.
- Installed plugins are copied into host-managed caches. A release change needs a canonical version
  bump and a host update/reinstall; editing the working tree does not modify an installed copy.

## Python and tests

- The dev toolchain requires Python 3.11+. Shipped plugin scripts must also run on Python 3.9
  because wrappers execute `python3` and stock macOS provides 3.9.6. Keep them stdlib-only and put
  3.10+ typing imports behind `if TYPE_CHECKING:`.
- Tests import skill scripts as top-level modules. Keep `tests/conftest.py` and
  `[tool.ty.environment].extra-paths` in `pyproject.toml` synchronized.
- Project state is `dict[str, Any]`, not `dict[str, object]`. Type all public functions.
- Tests live under `tests/plugins/<plugin>/<skill>/`; skills live under
  `plugins/<plugin>/skills/<skill>/`. Ruff line length is 120.

## Research workspace invariants

- Migration must not manufacture consent. Only a legacy `DONE` task carries authorization forward;
  migrate a `RUNNING` external task to `BLOCKED` with pending authorization.
- Validation sees arbitrary JSON before `validate_v3_state`; malformed input must produce findings,
  not exceptions. Use `read_text()` so decode errors become `WorkspaceError`.
- `project.json` is the commit point and is written last. Route post-commit index rebuilds through
  `_rebuild_index_after_commit`; normalize filesystem failures to `WorkspaceError`.
- Record task evidence with
  `research-project record-evidence <project-dir> --task <id> -- <command>`, and closure-step
  evidence — the report check, today the only step — with `--step <name>` from the fixed
  `CLOSURE_STEPS` vocabulary instead. The two are mutually exclusive: `--task` keeps its guard that
  the id exists in `project.json`, which is why a step that no task owns needs its own option rather
  than a reserved id. Never replace command output with prose or describe a failed command as
  passing.
