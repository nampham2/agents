# Choose the Python that runs a project script. Sourced by the three launchers, never run alone.
#
# The shipped scripts are stdlib only, so they need an interpreter and not a virtual environment.
# The floor lives here and nowhere else. The launchers target bash 3.2, the bash on stock macOS.

RESEARCH_PYTHON_FLOOR_MAJOR=3
RESEARCH_PYTHON_FLOOR_MINOR=11

# True when "$1" starts and reports a Python at or above the floor. A missing, broken or too old
# interpreter is false.
research_python_qualifies() {
  "$1" -c "import sys; raise SystemExit(sys.version_info < ($RESEARCH_PYTHON_FLOOR_MAJOR, $RESEARCH_PYTHON_FLOOR_MINOR))" \
    >/dev/null 2>&1
}

# Run the script "$1" with the arguments that follow it, on the newest qualifying Python on PATH.
# With none, run it through uv, which fetches a managed Python once. With neither, say so and fail.
# The exec calls replace the shell, so this function returns only when it failed to start anything.
research_run_python() {
  local script=$1
  shift
  local floor="$RESEARCH_PYTHON_FLOOR_MAJOR.$RESEARCH_PYTHON_FLOOR_MINOR"
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && research_python_qualifies "$candidate"; then
      exec "$candidate" "$script" "$@"
    fi
  done
  if command -v uv >/dev/null 2>&1; then
    # --quiet keeps stdout clean for the JSON that agents parse. A failed fetch shows uv's own
    # error and a nonzero exit; it never falls back to a Python below the floor.
    exec uv run --quiet --no-project --python ">=$floor" "$script" "$@"
  fi
  local found="no python3 on PATH"
  if command -v python3 >/dev/null 2>&1; then
    found="python3 is $(python3 -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo unknown)"
  fi
  echo "error: ${script##*/} needs Python $floor or later and none was found ($found)." >&2
  echo "Install Python $floor or later (for example 'brew install python'), or install uv" \
    "(https://docs.astral.sh/uv/) so this launcher can fetch one." >&2
  return 1
}
