# Goldfish review

A Goldfish is a fresh agent with no conversation history that reads one target and writes nothing.
A review in your own context never counts; with no fresh agent, ask the user for a waiver.

Each gate needs a current round: `spec` before the requirements confirmation; `planning` before
the planning confirmation; `design` before the first code task starts; `diff` before the release
and merge records (a no-remote worktree: before closure).

1. Run `workflow <project-dir> goldfish`, `operation: open`, with `target` (`diff` adds
   `worktree`) and `stages`. It returns the round and its token.
2. Start the reviewers in parallel as fresh general-purpose agents (Claude: non-fork `Agent`;
   Codex: spawn without conversation inheritance). Give each its section of
   [goldfish-personas.md](goldfish-personas.md) and absolute paths to the target and its links.
   Spec and delta rounds: comprehension, combined. Others by effort: low, combined; medium,
   comprehension, design critic, zoom-out, readiness; high, two of each critic.
3. Triage each finding: `FIX`, `CONTEST` (cite evidence the reviewer could read), `ESCALATE` (the
   user decides) or `DECLINE` (Nits only). Never contest a comprehension gap or change a
   severity. Apply fixes.
4. Ask the user only about `ESCALATE` items and Blockers or Majors still open.
5. `operation: close` with the token, a report per stage and the findings. After
   `reaudit-required`, `open` again: the one re-audit. Blockers or Majors left there need the
   user's words (`accepted-residual`).
6. A change after confirmation needs a delta round: comprehension, combined, given the previous
   report.
7. Merged before any diff round: `open` with `commit` set to the PR head; record the merge with
   `merged_head`. Push before a release; the tool never fetches.
8. No fresh agent: `operation: waive` with the user's words and the failure.

The tool records that this path ran, not that reviewers did.
