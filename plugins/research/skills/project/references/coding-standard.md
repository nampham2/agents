# Coding standard

Follow this on every code task, whether you work inline or as a delegated worker. Where the
repository's own rules are stricter, they win. Never loosen a rule here.

## Before you edit

Read the repository's contributor instructions and write down its own commands for lint, type
check, tests and coverage. Those are your gates. Look for every place the change must reach: other
copies of the logic, tests that pin the old value, callers.

## Tests

- Write the test first and run it against the unchanged code. Keep the command and the failure line.
  It fails for the right reason: an assertion about the behaviour, not an import error or a typo.
  For a pure refactor, run the existing tests before and after instead and say so.
- Test behaviour, not implementation. Cover the edge cases: empty, missing, duplicate, boundary.
  Test the refusal path and its message as well as the happy path.
- Keep tests free of the real clock, test order, network and credentials.
- Make sure each test can fail: check that a fixture is populated, and break a guard once to see
  the test go red.

## Code

- Keep the diff minimal and inside the scope you were given. Report any change outside it, with the
  reason.
- Keep each function you touch simple: a cyclomatic complexity of at most 10. Split a function
  before it passes that.
- Cover the lines and branches you changed: at least 90%, or more where the repository demands it.
  Read the coverage report for your changed lines, not only the total.

## Mutation testing

When a mutation testing tool (for example mutmut) is available, run it on the functions you
touched. Add tests that kill the surviving mutants. If a mutant is equivalent, meaning no
behaviour can tell it from the original, say why in your hand-back instead of adding a test.
If no tool is available, say so. Report the mutation result: tool, functions covered, mutants
killed, survived and equivalent.

## Gates

- Before handing back, run the repository's own lint, type check and test commands over the whole
  repository, tests included. A run of one file does not predict the full run.
- Run one command per gate. Do not pipe it into a filter or hide its exit code. Read the output
  even when the exit code is 0.
- Never pass a gate with casts, ignore comments, skipped tests, weakened assertions or lowered
  thresholds. Fix the code or the test. A conditional skip, with a stated reason, for a platform
  that is not available is allowed, and you report it.
- If a gate cannot be met, say which gate and why. Do not replace a check you cannot run with a
  weaker one.
- Re-run the gates after your last edit.

## Hand-back

Report what you did, in this order: the outcome in a sentence; the changed and new files; the test
that failed first, with its command and failure line; each gate with its command and exit code; the
mutation result; and what you did not verify, such as checks not run, code paths no test
exercised and facts you inferred rather than read. Do not claim a gate passed unless you ran it and
read its output.
