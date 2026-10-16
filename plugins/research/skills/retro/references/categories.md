# Candidate categories

A candidate is one change to the research plugin. Put it in one category. The category names the
surface that changes, so the fix project knows where to start.

| Category | The change | Use when |
| --- | --- | --- |
| `instructions` | A sentence in `SKILL.md` or a reference: unclear, contradictory, missing, or a no-op that moves no behavior. | A friction entry of kind `unclear-instruction`, `missing-information` or `reference-not-useful`. A refusal whose message the instructions did not predict. |
| `gates` | A check in the tool: one that should exist, fire earlier, name its cause, or stop firing. | A refusal that recurs across projects. A mistake that validation caught at closure instead of at the write. A `user-correction` that a check could have made. |
| `tool-economy` | The cost of a command or a payload: repeated calls, truncation, a read that returns more than the agent needs. | `invocations.by_command` is high for one command. A transcript shows many calls for one step. `wasted-command` entries. |
| `information-access` | A fact the agent needed that no record or command gave. | A `missing-information` entry. Evidence that failed because the agent guessed a path, a field or a token. |
| `workflow` | A step of the procedure that costs more than it returns. | The same step produces friction in several projects. A reflection says a step was skipped or repeated. |
| `tool-defect` | A bug: the tool did not do what its reference says. | A `tool-defect` entry. A crash in `invocations.failures`. |

## Check over rule

If a candidate can be a check in the tool, propose the check, not a sentence. A sentence in a
reference is read once per session and forgotten under context pressure. A check fires every time.
Reserve `instructions` for judgement the tool cannot make.

## Severity

- `high`: the friction recurs in 3 or more projects of the batch, or one occurrence cost a lost
  session, a wrong record or a destructive retry.
- `medium`: it recurs in 2 projects, or one occurrence cost several commands.
- `low`: one project, one or two commands.

Count recurrence from the rows. Do not estimate it.

## Evidence pointers

A pointer is `project`, `record` and an optional `anchor`:

- `invocations.jsonl` with the `at` instant of the line.
- `friction.md` with the entry id.
- `evidence.md` with the record id.
- `reflection.md` with the heading, for example `Retro`.
- `spec.md` with the decision id.
- `tasks/T01.md` with the finding id.

Read the record before you cite it. A pointer the fix project cannot open is not evidence.
