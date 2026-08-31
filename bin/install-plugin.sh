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

usage() {
  cat <<'USAGE'
usage: install-plugin.sh [--force] <plugin-name>

  --force  Register this clone as the marketplace even when that name is already declared with a
           different source. Without it the script refuses, rather than let Claude Code replace the
           existing declaration silently.
USAGE
}

PLUGIN=""
FORCE=0
while (($#)); do
  case $1 in
    --force) FORCE=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option '$1'" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n $PLUGIN ]]; then
        echo "error: unexpected argument '$1'" >&2
        usage >&2
        exit 2
      fi
      PLUGIN=$1
      ;;
  esac
  shift
done

if [[ -z $PLUGIN ]]; then
  usage >&2
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

# Marketplaces are keyed by name, so the one name this manifest declares holds exactly one source.
# Adding a directory source over a GitHub declaration is the direction the CLI does *not* guard: it
# succeeds and replaces the declaration without saying so, silently repointing anyone who installed
# from GitHub at this clone. Ask what is declared and refuse instead.
#
# Read it from the CLI, not from ~/.claude/settings.json: the settings layout is private, and --json
# is the interface offered for this question. It fails open — a failed call, a listing that will not
# parse, or an entry with no source at all prints nothing and the install proceeds — so a future
# format change costs this guard rather than every install. A source kind not known here is a
# different matter: it plainly is not this clone, so it is reported and refused.
conflicting_source() {
  local listing
  listing=$(claude plugin marketplace list --json 2>/dev/null) || return 0
  python3 - "$listing" "$MARKETPLACE" "$REPO_ROOT" <<'JSON'
import json, os, sys

try:
    entries = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)

name, root = sys.argv[2], sys.argv[3]
for entry in entries if isinstance(entries, list) else []:
    if not isinstance(entry, dict) or entry.get("name") != name:
        continue
    kind = entry.get("source")
    if kind == "directory":
        path = entry.get("path") or ""
        try:
            # samefile, so a symlinked or /private-prefixed clone is not read as a different one.
            same = os.path.samefile(path, root)
        except OSError:
            same = os.path.realpath(path) == os.path.realpath(root)
        if not same:
            print("a different directory (" + path + ")")
    elif kind == "github":
        print("GitHub (" + (entry.get("repo") or "unknown repository") + ")")
    elif kind:
        print(kind + " (" + (entry.get("url") or entry.get("path") or "unknown target") + ")")
    break
JSON
}

if DECLARED=$(conflicting_source) && [[ -n $DECLARED ]]; then
  if ((FORCE)); then
    echo "warning: replacing the '$MARKETPLACE' marketplace declaration ($DECLARED) with this clone" >&2
  else
    cat >&2 <<CONFLICT
error: marketplace '$MARKETPLACE' is already declared as $DECLARED. Pointing it at this clone would
       replace that declaration, and Claude Code does it silently. Pick one:

  - re-run with --force to replace it deliberately;
  - or work straight from this tree, which declares nothing:
      claude --plugin-dir $REPO_ROOT/plugins/$PLUGIN
CONFLICT
    exit 1
  fi
fi

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
