#!/usr/bin/env bash
set -euo pipefail
PLUGIN=${1:?usage: install-plugin.sh <plugin-name>}
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_SRC="$REPO_ROOT/plugins/$PLUGIN"
PLUGIN_DST="$HOME/.claude/plugins/$PLUGIN"

if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "error: plugin '$PLUGIN' not found at $PLUGIN_SRC" >&2
  exit 1
fi

mkdir -p "$(dirname "$PLUGIN_DST")"
ln -sfn "$PLUGIN_SRC" "$PLUGIN_DST"
echo "installed: $PLUGIN_DST -> $PLUGIN_SRC"
