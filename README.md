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

This declares `agents` as a *directory* marketplace pointing at this clone. If `agents` is already
declared as a GitHub marketplace, Claude Code replaces that declaration without warning, so the
script refuses the switch unless you pass `--force` (see below).

Symlinking a plugin into `~/.claude/plugins/` does *not* register anything — `claude plugin list`
still reports `No plugins installed`.

Skills are discovered from `skills/<name>/SKILL.md` and addressed as `<plugin>:<skill>` — for
example `research:project`.

## Install from GitHub

The marketplace also resolves straight from the remote, which is how to reach it from a machine with
no clone of this repo:

```bash
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

`marketplace add` accepts a URL, a local path, or an `owner/repo` GitHub reference. This
repository is **private**, so git needs credentials that can read it — the same ones `git clone`
would use. When they are missing, the failure surfaces as a git authentication error rather than
as a marketplace error, which is worth knowing before you go hunting for a problem in the manifest.

This installs whatever is on the default branch, not your working tree. To work against the tree
instead, see [Working on an installed plugin](#working-on-an-installed-plugin).

### Only one of the two routes at a time

`.claude-plugin/marketplace.json` names this marketplace `agents`, and Claude Code keys marketplaces
by name, so the name holds exactly one source. Both routes above claim it: the local one declares
`agents` as a directory, the GitHub one as `nampham2/agents`. They are mutually exclusive, and
following this section after the one above it fails:

```text
✘ Failed to add marketplace: Cannot add marketplace "agents": its network source differs from the
  one declared for it in settings
```

Switch by dropping the declaration first:

```bash
claude plugin marketplace remove agents     # also uninstalls research@agents
claude plugin marketplace add nampham2/agents
claude plugin install research@agents       # reinstall — the remove above cascaded
```

The reinstall is not optional: removing a marketplace uninstalls the plugins that came from it.

The guard only fires in this direction. Adding the *directory* source over a GitHub declaration
succeeds silently and replaces it, which is why `bin/install-plugin.sh` checks for itself and refuses
without `--force`.

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
