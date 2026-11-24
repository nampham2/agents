# Coding worker

Read only by a worker on a code task; the packet names this file by absolute path. It adds coding
practice to the worker instructions of [task-workers.md](task-workers.md). The worker instructions
still apply. Read [coding-standard.md](coding-standard.md) first: its rules apply in full and this
file adds the packet and report rules on top of it, without repeating them. Where the target
repository's rules are stricter than this file or that one, the repository's rules win; they never
loosen a floor.

## Before you edit

- Read the repository's `AGENTS.md` or `CLAUDE.md` before you edit. Read them at the root and in
  the folders you will change. If neither exists, read the development or contributing section
  of the README, then the hook, CI and project configuration. Write down the exact commands for
  lint, format check, type check, tests and coverage: these are your gates.
- Work only in the directory the packet names. Run `git status --short --branch` there first and
  keep the output as your baseline. Use absolute paths, and run each tool from its repository.
- Treat every claim in the brief, the design, a review or a docstring as a hypothesis until you
  have read the code behind it. If the code disagrees with the brief, stop and ask the coordinator.
- Do not fetch or switch branches. When a fact comes from another repository or a dependency,
  note its ref and date in your report.
- Before you start, search for every place the change must reach: other copies of the logic,
  files that state the same value, tests that pin the text or number, callers and consumers.
- Settle an uncertain premise about a library, parser or tool with a small run in a scratch file
  outside the repository, not with reasoning.

## While you edit

- Test first for any change of behaviour, a defect fix or a new behaviour: write the test, run it
  on the unchanged code, and keep the command, exit code and failure line. It fails for the right
  reason: an assertion about the behaviour, not an import error or a typo. Two exceptions only,
  and your report names the one you used and why:
  - a pure deletion or refactor with no change of behaviour: run the existing tests before and
    after as the control;
  - a new interface for which no behaviour-level test can exist before the code: write the test
    with the code and show it failing against a deliberately wrong input.
- Make sure each test can fail: check that a fixture is populated, count the items before you
  assert about each, and for a guard that matters break it once and see the test go red.
- Test the shapes real callers send and the edge cases: empty, missing, duplicate, boundary. Keep
  tests free of the real clock, test order, network and credentials.
- Keep the diff minimal and inside your assigned paths. A defect inside your scope is yours to fix.
  A change outside your paths is made only when the brief allows it, and is reported with its
  reason. Read code and tests before you delete them.
- Run formatters and auto-fixers on your own files only, unless the repository's rules say
  otherwise and the unchanged tree is already clean under them.
- Do not pass a gate with casts, ignore comments, skipped tests, weakened assertions or lowered
  thresholds. If a gate cannot be met, say which and why.
- Run no Git command that changes HEAD, the index or the stash: no checkout, switch, reset,
  rebase, stash, fetch, pull or add. Read-only commands such as `status`, `diff` and `log` are fine.
- Prefer exact-match edits. A scripted edit matches a whole unique line, and you check the result.
  Never rebuild a file from truncated output. Keep scratch files outside the repository.
- When you fix a check or a refusal, test the refusal path and its message, and its sibling paths.

## Before you hand back

- Run every repository gate with the repository's own commands and scope, over the whole
  repository, tests included. Targeted runs are for iterating; they do not predict the full run.
- Run one command per gate. Do not pipe a check into a filter, chain steps with `;` or append
  `|| true`, which hide the exit code. Read the output even when the exit code is 0.
- Check the working-tree copy, not an installed one: call a shipped tool by its path and confirm
  which interpreter and dependencies ran. A check your environment cannot run is reported as not
  run, never replaced by a weaker one.
- Report your new files to the coordinator, who adds them: the hooks see only tracked files. Do
  not run `git add`. When the change has a local entry point without side effects, run it once on
  realistic input and read the result.
- When a mutation testing tool is available, run it on the functions you touched and act on the
  survivors, as the coding standard says.
- Re-run the affected gates after your last edit, a formatter run included. Do not retry a
  failure that cannot change: read the error first.

## Your report

This list extends the result format of [task-workers.md](task-workers.md). Give each item, so the
coordinator can check it line by line; it re-runs the gates and does not accept a claim alone.

1. Outcome, in one or two sentences.
2. Changed, new and deleted paths; `git status --short --branch`; that nothing was committed;
   files you created outside the repository.
3. The failing-first run: command, exit code, the failure in one line; or the exception you used.
4. Each gate: command, directory, exit code, failures included; and the mutation result.
5. Not verified: checks not run and why, code paths no test exercised, facts inferred.
6. Deviations from the brief or design, changes outside your paths, decisions you made.
7. Text your change makes false outside your paths; one line for the next worker, if any.
