# Quality gates

A gated project cannot mark a code task DONE until its approved checks pass. A code task is a
`local_write` task with a non-prose file in a `target` output.
The design and diff gates also count an `external` or `destructive` task with a required non-prose
`target` output.

## Actions

Send each with `research-project workflow <project-dir> <action> -`. Add `--schema` to see the keys.

| Action | Input | Result |
| --- | --- | --- |
| `gate-discover` | `{}` | Candidate commands and source files from the target repository. Writes nothing. |
| `gates` | `operation` (`approve` or `waive`), the user's `confirmation` (`source`, `response`) | `approve` adds `commands`, each `{id, kind, argv}` with `kind` `lint`, `types`, `tests` or `format`. `waive` adds `kinds` and `reason`. |
| `gate-run` | `task` | Runs the approved commands, the diff scan, diff coverage and complexity. Writes `observability/<task>.json` and a finding. |

`gate-run` needs no `expected_revision`; `gates` does.

## Workflow

1. `gate-discover`.
2. Show the user the commands. Run `gates` approve with the user's own words in `confirmation`.
3. `task start`, then the work.
4. `gate-run` for the task.
5. `task finish`.

Approval needs a `lint`, a `types` and a `tests` command, or a waiver for each missing kind. The
base is the repository head at the first approval; every run covers all changes since it.

Gates run in the recorded worktree that holds the task's changes, else the `target` entry. With
neither, the run refuses with `gates-ambiguous-worktree`, naming the candidates. A `target`
worktree may be recorded from a repository subdirectory.

## Refusal

`task finish` is refused on every path that could mark the task DONE (`update`, `reconcile`,
`correct`, backfill). The message names the cause: `gates-not-approved`,
`gate-not-run`, `gate-failed`, `gates-stale`, `gates-source-changed`, `bypass-added`,
`floor-lowered`, `coverage-missing`, `gates-subdirectory`, `gates-no-repository`,
`gates-snapshot` or `gates-not-code-task`. A change to the tree after a run makes it stale.
Floors: diff coverage 90%, complexity 10 per touched function.

`gate-run` refuses a task that is not a code task. Once a task has
started, its outputs and effect are locked, even after it goes back to TODO. To change them, skip
the task and add a new one.

## Waivers

`gates` waive needs the user's words. A kind of `lint`, `types`, `tests` or `format` waives that
command kind; the diff scan still runs. `no-repository` is for a target that is not a repository
and skips the whole run.

## Validation warnings

`validate` and `research-validate` warn, never fail, when a DONE code task of a gated project has
no valid record, and name the fix. They warn once when a record lists no Python file. A task
finished before approval and a `no-repository` project are exempt.

## Cautions

- A repository whose `addopts` has `--cov` needs an explicit `--cov=<dir>` in the approved
  command; otherwise the run adds `--cov=.`.
- The diff scan reads string literals, so fixture lines holding `noqa` or `type: ignore` trip
  `bypass-added`. A changed `.gitattributes` trips it too. Only `typing.cast` and `cast` imported
  from `typing` or `typing_extensions` are caught. `cast (x, y)` and re-exports pass.
- An approved `--no-cov`, `-p no:cov` or own `--cov-report=json` is refused.

## Limits

- The gate guards against honest mistakes, not a worker who can write the project folder.
- The base is set at the first approval and nothing moves it. After a rebase or a merge of main,
  start a new project.
- A finish takes more than one snapshot of the tree, under the project lock. Ignore large
  generated folders.
- Paths ignored by `.gitignore` or a global ignore file and never staged are invisible to the
  scan and the hash.
- Only Python is scanned. A finish judges the task's declared outputs.
- The outputs lock reads `started_at` and falls back to the status where no stamp exists. A task
  that reached RUNNING without a stamp and was then sent back to TODO is not locked.
- `# pragma: no cover` on an `if __name__` or `TYPE_CHECKING` line is information, not a finding.
  Logic under that guard escapes diff coverage.
- A changed file over 8 MiB is not read; a Python or config one is a `bypass-added` finding.
- New projects are gated unless `RESEARCH_NEW_PROJECT_GATES` is exactly `0`.
- A `no-repository` waiver is not rechecked once a repository is recorded.
- The `tests` kind must be pytest; other runners can only be waived.
- The target must be the repository root.
- A gitlink (a submodule or nested repository) is one changed path, never read. A new commit in it
  changes the hash. Uncommitted edits inside it are invisible. A nested repository with no commit
  stops the run with `git add failed ... adding files failed`; commit in it or move it out.
