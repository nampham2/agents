# Grill

`grill` is an internal component of the `research` plugin, not a command. It interviews the user
about a plan, design, or decision until the goal is genuinely shared, then stops and hands back. It
exists because the expensive failure in agent work is not bad code: it is an agent that was
confidently building the wrong thing, and a plan the user nodded at rather than decided.

This page is for contributors. `project` invokes `grill` for every project's initial requirements
alignment and revisits affected decisions during iterative architecture review. Clear requests use
zero question rounds, a shared summary, and explicit confirmation. Claude hides its
slash command; Codex still exposes it — see [Why it is hidden](#why-it-is-hidden).

## How a session runs

The subject is modelled as a tree of decisions. Each **round** asks the whole **frontier** — every
decision whose prerequisites are already settled — so the user is never asked something that hinges
on an answer nobody has given yet. Answers push the frontier outward and the next round asks what
that unblocked. A dozen questions usually land in about three rounds.

Every question carries a recommended answer, so the user can agree, disagree, or answer by number
without reconstructing the reasoning first. Closed-ended choices use the host's available question
tool, falling back to plain text; open-ended ones are numbered text.

Facts are the agent's job: anything the filesystem, the repository, or the documentation can settle
gets looked up rather than asked, and is reported with its source so a wrong one can be corrected.
Decisions are the user's, and the session waits for them. Questions that cannot be settled by talking
at all are named **ungrillable** and answered by building the smallest throwaway thing instead.

The session ends with an explicit confirmation. Running out of questions is not consent, "no
objection" is not agreement, and agreement about what to do is never authorization to do it.

## What it writes

Everything `grill` records lands in the project workspace, never here:

- the agreed specification, in the project's `spec.md` under `## Current specification`, as the seven
  `###` sections `research-validate` warns about individually;
- one dated entry in `## Decision history` per settled branch, plus one recording the confirmation;
- nothing at all in `briefing.md`, which the briefing step owns. A fact the interview contradicts is
  a dated correction in `spec.md`, not a rewrite of the briefing.

Requirements confirmation hands back to `project` for architecture review. The review can reopen
requirements and return to grill until both agent and user agree on the architecture and effort.
The initial project stays in `ALIGNING` until both confirmations are recorded and the agreed
`architecture.md` is produced. Grill updates requirements; project maintains the architecture document.
See [the architecture review procedure](../project/references/architecture-review.md).

## Why it is hidden

`SKILL.md` carries `user-invocable: false`, which hides the slash command while leaving the skill
reachable by the model through the Skill tool — so `project` can invoke it during alignment.
The frontmatter `description` was narrowed to project-lifecycle triggers at the same time, because
the flag alone would still have let a plain-English "grill me on this plan" start a standalone
session that no longer has anywhere to record its result.

`user-invocable` is a Claude Code key. Codex parses only `name`, `description`, and
`disable-model-invocation`, so `/research:grill` remains typable there. See
the cross-host plugin contract in the repository `AGENTS.md`.

Two consequences for anyone editing this skill:

- do not add an ``Invoke with `/…` `` line back. `tests/plugins/research/test_skill_docs.py` fails a
  skill that is not user-invocable but still claims an invocation, and fails a normal skill that
  lacks one;
- do not reach for `disable-model-invocation` as the lever. It is the opposite flag, and it would
  break `project`.

For the full instruction set, see [`SKILL.md`](SKILL.md).
