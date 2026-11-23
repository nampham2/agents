# Quality gates

A gated project cannot mark a code task DONE until its approved checks pass. A code task is a
`local_write` task with a non-prose file in a `target` output. New projects are gated; older ones
are not.

## Actions

Send each with `research-project workflow <project-dir> <action> -`. Add `--schema` to see the keys.

| Action | Input | Result |
| --- | --- | --- |
| `gate-discover` | `{}` | Candidate commands and source files from the target repository. Writes nothing. |
| `gates` | `operation` (`approve` or `waive`), the user's `confirmation` (`source`, `response`) | `approve` adds `commands`, each `{id, kind, argv}` with `kind` `lint`, `types`, `tests` or `format`. `waive` adds `kinds` and `reason`. |
| `gate-run` | `task` | Runs the approved commands, the diff scan, diff coverage and complexity. Writes `observability/<task>.json` and a finding. |

The `tests` command must be pytest. It needs no `expected_revision`; `gates` does.

## Workflow

1. `gate-discover`.
2. Show the user the commands. Run `gates` approve with the user's own words in `confirmation`.
3. `task start`, then the work.
4. `gate-run` for the task.
5. `task finish`.

Approval needs a `lint`, a `types` and a `tests` command, or a waiver for each missing kind. The
base is the repository head at the first approval. Every run covers all changes since that base.

## Refusal

`task finish` is refused on every path that could mark the task DONE: `task finish`, backfill,
`update`, `reconcile` and `correct`. The message names the cause: `gates-not-approved`,
`gate-not-run`, `gate-failed`, `gates-stale`, `gates-source-changed`, `bypass-added`,
`floor-lowered` or `coverage-missing`. The fix is to start the task, run `gate-run`, then finish.
Any change to the tree after a run makes it stale. Floors: diff coverage 90%, complexity 10 per
touched function.

## Waivers

`gates` waive needs the user's words. A kind of `lint`, `types`, `tests` or `format` waives that
command kind, for a repository with no such tool. The kind `no-repository` is for a target that is
not a repository and takes the place of every run. A waiver never covers the diff scan.

## Validation warnings

`validate` and `research-validate` warn, and never fail, when a DONE code task of a gated project
has no valid record. They name the fix. They warn once when a record lists no Python file: the
gates cover Python only. A task finished before approval and a `no-repository` project are exempt.

## Cautions

- Approving `uv run pytest -q` in a repository whose `addopts` already has `--cov` needs an
  explicit `--cov=<dir>`. Otherwise the run adds `--cov=.` to it.
- The diff scan reads string literals. A repository whose own tests hold `noqa` or `type: ignore`
  fixture lines trips `bypass-added`.
- An approved `--no-cov`, `-p no:cov` or own `--cov-report=json` is refused.
