#!/usr/bin/env bash
# Install a plugin from this repo into Claude Code.
#
# Claude Code registers plugins through a *marketplace*, not by directory placement: this repo
# root is a directory marketplace (.claude-plugin/marketplace.json), which is added and then
# installed from. Symlinking a plugin into ~/.claude/plugins/ registers nothing at all —
# `claude plugin list` still reports "No plugins installed" — which is how the previous version
# of this script managed to print success while installing nothing.
#
# Both CLI steps are idempotent, so re-running this is safe.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/.claude-plugin/marketplace.json"

PLUGIN=${1-}
if [[ -z $PLUGIN ]]; then
  echo "usage: install-plugin.sh <plugin-name>" >&2
  exit 2
fi

# The name is both a path component and a CLI argument, so accept only a bare name: the old
# script passed it straight into a path and `../tests` wrote outside the destination directory.
if [[ ! $PLUGIN =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "error: '$PLUGIN' is not a valid plugin name — pass a bare name, not a path" >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "error: the 'claude' CLI is not on PATH; it is what performs the install" >&2
  exit 1
fi

if [[ ! -f $MANIFEST ]]; then
  echo "error: no marketplace manifest at $MANIFEST" >&2
  exit 1
fi

# The marketplace, not the directory listing, is the authority on what is installable.
read_manifest() {
  python3 - "$MANIFEST" "$PLUGIN" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
names = [p.get("name") for p in manifest.get("plugins", [])]
if sys.argv[2] not in names:
    print(" ".join(n for n in names if n), file=sys.stderr)
    sys.exit(1)
print(manifest["name"])
PY
}

if ! MARKETPLACE=$(read_manifest); then
  echo "error: '$PLUGIN' is not published by this marketplace ($MANIFEST)" >&2
  exit 1
fi

# Validate here rather than in CI: this is the one place the CLI is guaranteed present, and an
# invalid manifest is exactly what previously shipped unnoticed.
claude plugin validate --strict "$REPO_ROOT"
claude plugin validate --strict "$REPO_ROOT/plugins/$PLUGIN"

claude plugin marketplace add "$REPO_ROOT"
claude plugin install "$PLUGIN@$MARKETPLACE"

cat <<NOTE

Installed $PLUGIN@$MARKETPLACE from this working tree. Verify with:

  claude plugin list

Skills are discovered from skills/<name>/SKILL.md and are addressed as <plugin>:<skill> — for
example $PLUGIN:workbench.

Installing copied the plugin into a version-keyed cache, so later edits to this working tree do
NOT reach it. 'claude plugin marketplace update' refreshes the listing only, not the cache.

  - live development, straight from this tree:  claude --plugin-dir $REPO_ROOT/plugins/$PLUGIN
  - publish a change to this installed copy:    bump "version" in
                                                plugins/$PLUGIN/.claude-plugin/plugin.json, then
                                                claude plugin marketplace update $MARKETPLACE
                                                claude plugin update $PLUGIN@$MARKETPLACE
NOTE
