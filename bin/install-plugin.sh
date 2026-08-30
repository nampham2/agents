#!/usr/bin/env bash
# Install a plugin from this repo into Claude Code.
#
# Claude Code registers plugins through a *marketplace*, not by directory placement: this repo
# root is a directory marketplace (.claude-plugin/marketplace.json), which is added and then
# installed from. Symlinking a plugin into ~/.claude/plugins/ registers nothing at all —
# `claude plugin list` still reports "No plugins installed" — which is how the previous version
# of this script managed to print success while installing nothing.
#
# Re-running is safe, and on an already-installed plugin it refreshes rather than no-ops: the
# previous version always ran `plugin install`, which the CLI treats as satisfied when the plugin is
# present, so editing this tree and re-running printed success while the installed copy stayed as it
# was. Installs are copied into a cache keyed on the plugin's version, so a refresh only reaches that
# copy when the version in plugin.json has changed — this script refuses rather than pretend.
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

# The version in the plugin manifest is what the install cache is keyed on, so it decides whether a
# refresh can reach the installed copy at all.
manifest_version() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$1"
}

# Ask the CLI what is installed rather than guessing from the filesystem: the cache layout is the
# CLI's business, and `plugin list --json` is the interface it offers for exactly this question.
installed_version() {
  local listing
  listing=$(claude plugin list --json 2>/dev/null) || return 0
  python3 -c '
import json, sys
try:
    entries = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
for entry in entries if isinstance(entries, list) else []:
    if isinstance(entry, dict) and entry.get("id") == sys.argv[2]:
        print(entry.get("version", ""))
        break
' "$listing" "$1"
}

PLUGIN_MANIFEST="$REPO_ROOT/plugins/$PLUGIN/.claude-plugin/plugin.json"
if ! VERSION=$(manifest_version "$PLUGIN_MANIFEST"); then
  echo "error: $PLUGIN_MANIFEST has no readable \"version\"; the install cache is keyed on it" >&2
  exit 1
fi

# Validate here rather than in CI: this is the one place the CLI is guaranteed present, and an
# invalid manifest is exactly what previously shipped unnoticed.
claude plugin validate --strict "$REPO_ROOT"
claude plugin validate --strict "$REPO_ROOT/plugins/$PLUGIN"

claude plugin marketplace add "$REPO_ROOT"

INSTALLED=$(installed_version "$PLUGIN@$MARKETPLACE")

if [[ -z $INSTALLED ]]; then
  echo "==> $PLUGIN@$MARKETPLACE is not installed; installing version $VERSION"
  claude plugin install "$PLUGIN@$MARKETPLACE"
  ACTION="installed version $VERSION"
elif [[ $INSTALLED == "$VERSION" ]]; then
  cat >&2 <<STALE
error: $PLUGIN@$MARKETPLACE is already installed at version $VERSION, and the install cache is keyed
       on that version, so refreshing it would copy nothing and report success. Pick one:

  - bump "version" in plugins/$PLUGIN/.claude-plugin/plugin.json, then re-run this script;
  - or work straight from this tree, no install involved:
      claude --plugin-dir $REPO_ROOT/plugins/$PLUGIN
STALE
  exit 1
else
  echo "==> $PLUGIN@$MARKETPLACE is installed at version $INSTALLED; refreshing to $VERSION"
  claude plugin marketplace update "$MARKETPLACE"
  claude plugin update "$PLUGIN@$MARKETPLACE"
  ACTION="refreshed $INSTALLED -> $VERSION"
fi

cat <<NOTE

$PLUGIN@$MARKETPLACE: $ACTION, from this working tree. Verify with:

  claude plugin list

Skills are discovered from skills/<name>/SKILL.md and are addressed as <plugin>:<skill> — for
example $PLUGIN:project. A restart is required before the change is live.

Installing copies the plugin into a version-keyed cache, so later edits to this working tree do NOT
reach it, and 'claude plugin marketplace update' refreshes the listing only, not the cache. To keep
editing without reinstalling:

  claude --plugin-dir $REPO_ROOT/plugins/$PLUGIN
NOTE
