# Workspace records layout

The four cross-project views, `INDEX.md`, `MEMORY.md`, `POSTMORTEMS.md` and `RETRO.md`, are derived
from per-entry sources (`<id>/project.json`, `<id>/reflection.md`, `memory/<slug>.md`,
`retro/<id>.md`). In a migrated workspace they are untracked and git-ignored, so closes touch
disjoint tracked files and never conflict on a view.

## Root marker and self-repair

A directory is a workspace root when it has a canonically named project directory and either
`.workspace-root` or `INDEX.md`. The marker is committed; only its existence is tested, and its
content (`{"layout":1}`) is a reserved version. With the marker present, `validate --check-index`
does not report a stale view as an error, because views repair themselves:

- `research-project context`, `workflow resume` and `rebuild-index` rebuild missing or stale views
  under `.index.lock`. `ensure_views` never fails a read; on a read-only root it reports not-ok and
  callers fall back to `render_views`, an in-memory render.
- A fresh clone has no views; run `research-project rebuild-index <workspace-root>` or any of the
  commands above.
