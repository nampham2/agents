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

`gate-run` needs no `expected_revision`; `gates` does.

## Workflow

1. `gate-discover`.
2. Show the user the commands. Run `gates` approve with the user's own words in `confirmation`.
3. `task start`, then the work.
4. `gate-run` for the task.
5. `task finish`.

Approval needs a `lint`, a `types` and a `tests` command, or a waiver for each missing kind. The
base is the repository head at the first approval. Every run covers all changes since that base.

## Refusal

`task finish` is refused on every path that could mark the task DONE (`update`, `reconcile`,
`correct` and backfill too). The message names the cause: `gates-not-approved`,
`gate-not-run`, `gate-failed`, `gates-stale`, `gates-source-changed`, `bypass-added`,
`floor-lowered` or `coverage-missing`. The fix is to start the task, run `gate-run`, then finish.
Any change to the tree after a run makes it stale. Floors: diff coverage 90%, complexity 10 per
touched function.

## Waivers

`gates` waive needs the user's words. A kind of `lint`, `types`, `tests` or `format` waives that
command kind, for a repository with no such tool; the diff scan still runs. The kind `no-repository`
is for a target that is not a repository and skips the whole run.

## Validation warnings

`validate` and `research-validate` warn, and never fail, when a DONE code task of a gated project
has no valid record, and name the fix. They warn once when a record lists no Python file: the gates
cover Python only. A task finished before approval and a `no-repository` project are exempt.

## Cautions

- A repository whose `addopts` already has `--cov` needs an explicit `--cov=<dir>` in the approved
  command. Otherwise the run adds `--cov=.`.
- The diff scan reads string literals, so fixture lines holding `noqa` or `type: ignore` trip
  `bypass-added`. A changed `.gitattributes` trips it too.
- An approved `--no-cov`, `-p no:cov` or own `--cov-report=json` is refused.

## Limits

- The gate guards against honest mistakes, not a worker who can write the project folder. Its
  evidence is hashes stored beside the data; whoever can write one can write the other.
- The base commit is set at the first approval and never moves. After a rebase or a merge of main,
  edit the project record or start a new project.
- A finish takes more than one snapshot of the tree and runs under the project lock. Ignore large
  generated folders in `.gitignore`.
- New projects are gated by default. `RESEARCH_NEW_PROJECT_GATES=0` (exactly `0`) turns that off.
- A `no-repository` waiver is not checked again after a repository is recorded.
- The `tests` kind must be a pytest command. Other runners can only be waived.
- The target must be the repository root. A submodule shows as one changed path, not its files.
