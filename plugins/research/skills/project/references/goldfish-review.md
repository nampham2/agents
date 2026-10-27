# Goldfish review

A Goldfish is a fresh agent with no conversation history that reads one target and writes nothing.
A review in your own context never counts.

Each gate needs a current round: `spec` before the requirements confirmation; `planning` before
the planning confirmation; `design` before the first code task starts; `diff` before the release
and merge records (a no-remote worktree: before closure).

1. Run `workflow <project-dir> goldfish`, `operation: open`, with `target` (`diff` adds
   `worktree`, its recorded path) and `stages`. Keep `round`, `token` and `reviewed`.
2. Start one fresh general-purpose agent per reviewer, in parallel (Claude: non-fork `Agent`;
   Codex: `fork_context: false`). Give each all of [goldfish-personas.md](goldfish-personas.md),
   its persona, and absolute paths to the target and what it links. A diff reviewer also gets
   the `reviewed` commit and the base; a re-audit or delta reviewer, the previous round's document.
3. Stages: spec and delta rounds `comprehension`, `combined`; else by the plan's effort: low
   `combined`; medium `comprehension`, `design-critic`, `zoom-out`, `readiness`; high the same,
   two agents per critic, merged into that stage's one report.
4. Triage each finding: `FIX`, `CONTEST` (cite evidence the reviewer could read), `ESCALATE` (the
   user decides) or `DECLINE` (Nits only). Never contest a comprehension gap or change a
   severity. Apply fixes; commit them before closing a diff round.
5. Ask the user only about `ESCALATE` items and Blockers or Majors still open.
6. `operation: close` with `tokens` `{"reviews/goldfish_NN": token}`, `reports`, `findings`, and a
   `confirmation` for contested or escalated Blockers or Majors and for `accepted-residual`.
   After `reaudit-required`, `open` again: the one re-audit.
7. A pull request merged with no diff round: open a diff round with `commit` set to the PR head,
   then record the merge with `merged_head`.
8. No fresh agent: `operation: waive` with `reason` and the user's `confirmation`.
