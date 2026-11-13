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

## Size tiers

| Size | Where it lives |
| --- | --- |
| Under 1 MiB | Plain git, anywhere in the workspace. |
| Irreplaceable, 1 MiB or more | `workspace/<id>/artifacts/large/`, tracked by Git LFS through one root `.gitattributes` line. |
| Regenerable, 1 MiB or more | Git-ignored, listed in `workspace/<id>/artifacts/MANIFEST.json`. |

The `.gitattributes` line is `<prefix>/*/artifacts/large/** filter=lfs diff=lfs merge=lfs -text`,
where `<prefix>` is the workspace path relative to the git root. Nested `.gitattributes` files are
not used. `MANIFEST.json` is `{"files": [{"path": "...", "size": 123}]}` (a bare list also works);
`path` is relative to the project's `artifacts/` directory and `path` and `size` are required.
`sha256` and `rebuild` are free-form hints. GitLab rejects non-LFS blobs over 5 MiB at push, so
doctor treats 4 MiB as the error line.

## `research-project doctor [<workspace-root>]`

Read-only. Prints `error|warning <rule> <path>: <message>; fix: <remediation>` per finding. Exit
0 when there are no errors, 1 when any error, 2 when the directory or git is unreadable. A
directory outside a git repository skips R1-R3, R5 and R6. Only blobs not in HEAD are judged, so
files already committed are untouched.

| Rule | Severity | Finding |
| --- | --- | --- |
| R1 | error | Non-LFS blob over 4 MiB staged but not in HEAD, or untracked and not ignored. |
| R2 | warning | New file of 1 to 4 MiB outside `artifacts/large/`. |
| R3 | error | `filter.lfs.clean`, `smudge` or `process` unset, or `git lfs version` fails. |
| R4 | warning | `.<name>.*.tmp` at the workspace root or in a project directory. |
| R5 | error | Layout incomplete: marker, ignored views, `.gitignore` or `.gitattributes` lines. |
| R6 | warning | Ignored file over 1 MiB under `artifacts/` and absent from `MANIFEST.json`. |

R3 fix: run `git lfs install --local` in the repository.

## Pre-commit hook

Doctor is not installed as a hook. To enforce it, save this as `.git/hooks/pre-commit` and make it
executable:

```sh
#!/bin/sh
exec research-project doctor "$(git rev-parse --show-toplevel)/workspace"
```

It fails closed: any error finding, or an unreadable tree, rejects the commit. It resolves
`research-project` through `PATH`, and `git commit --no-verify` bypasses it.

## Fresh clone

Git-ignored views and LFS config do not travel with a clone. After cloning, run
`git lfs install --local`, then `research-project rebuild-index <workspace-root>`. A plugin older
than this layout finds a root only through `INDEX.md`; update the plugin, or run `rebuild-index`
once, before relying on a fresh clone.
