# Grill

`grill` is a Claude skill that interviews you about a plan, design, or idea until the goal is
genuinely shared, then stops. It exists because the expensive failure in agent work is not bad code:
it is an agent that was confidently building the wrong thing, and a plan you nodded at rather than
decided.

Use it through prompts with `/grill`. `project` also runs it during alignment and when review
feedback changes a requirement.

## How a session runs

The subject is modelled as a tree of decisions. Each **round** asks the whole **frontier** — every
decision whose prerequisites you have already settled — so you are never asked something that hinges
on an answer Claude has not heard yet. Your answers push the frontier outward and the next round asks
what that unblocked. A dozen questions usually land in about three rounds.

Every question comes with a recommended answer, so you can agree, disagree, or answer by number
without reconstructing the reasoning first. Closed-ended choices arrive as selectable options with
previews; open-ended ones arrive as numbered text.

Facts are Claude's job. Anything the filesystem, the repository, or the documentation can settle gets
looked up rather than asked, and is reported with its source so you can correct it. Decisions are
yours, and the session waits for them.

## Starting a session

```text
/grill I want to add a caching layer to the API but I have not worked out where it belongs.

/grill Stress-test this migration plan before I commit to it: <plan>

/grill Should this be one plugin or three? I keep going back and forth.
```

Start from a loose idea. Not knowing what the work involves is the normal starting condition, and
"I don't know" is a real answer — usually a signal to build a throwaway version rather than keep
talking.

## What it will not do

- Answer its own questions. Reaching the end of the questions is not consent.
- Treat silence or "no objection" as agreement. Confirmation is always explicit.
- Start building because the interview went well. Agreement about what to do is not authorization to
  do it.
- Cap the questions. Some subjects need three and some need forty. If a session is running long the
  scope is usually too large: ask to split it and grill the pieces.

## Ungrillable questions

Some questions cannot be settled by talking, because you need something to react to first: how an
interaction should feel, whether one long page beats three short ones. Grill names these instead of
circling them, and suggests the smallest throwaway thing that would answer one. Build that, look at
it, come back and answer in a line.

## Standalone and inside the project skill

Standalone, `/grill` is stateless: no workspace, no files, nothing left on disk. The result is a
sharper idea and a confirmed understanding in the conversation. If the subject turns out to deserve a
persistent project, hand the same conversation to `/research:project` — most of the frontier is already
settled, so alignment there is short.

Inside `project`, the consensus is recorded rather than remembered: the agreed specification is
written to the project's `spec.md`, each settled branch becomes a dated decision, and the project
cannot leave `ALIGNING` until your confirmation is on record. When review feedback changes a
requirement, only the affected branch is re-grilled.

## Prompts that steer it

```text
Ask one question at a time.            (kept for the rest of the session)
That question is beneath the fidelity I need — go deeper on <topic>.
Scope is drifting. Park <branch> and stay on <branch>.
Wrap up: summarize where we are and what is still open.
```

For the full instruction set, see [`SKILL.md`](SKILL.md).
