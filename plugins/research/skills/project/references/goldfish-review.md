# Goldfish review

A Goldfish is a fresh agent with no history; it reads one target, writes nothing.
Self-review never counts.

Each gate needs a current round: `spec` before the requirements confirmation; `planning` before
the planning confirmation; `design` before the first code task starts; `diff` before the release and
merge records and, per target, before closure.

1. Run `workflow <project-dir> goldfish`, `operation: open`, with `target` (`diff` adds
   `worktree`, its recorded path) and `stages`. Keep `round`, `token`, `reviewed`.
2. Start one fresh agent per reviewer (Claude: non-fork `Agent`; Pi: `subagent`,
   `context: "fresh"`); call: [task-workers.md](task-workers.md). Give each
   [goldfish-personas.md](goldfish-personas.md) whole, its persona, absolute paths to the target and
   its links. A diff reviewer also gets
   the `reviewed` commit and the base; a re-audit or delta reviewer, the previous round's document.
3. Stages: policy 3+ follows [review-risk.md](review-risk.md). Earlier: spec and planning
   `combined`; design and diff by effort: low `combined`; medium `comprehension`, `design-critic`,
   `zoom-out`, `readiness`; high the same, two agents per critic, a merged report; a delta
   adds `comprehension`.
4. Triage each finding: `FIX`, `CONTEST` (cite evidence the reviewer could read), `ESCALATE` (the
   user decides) or `DECLINE` (Nits only). Never contest a comprehension gap or change a severity.
   Commit fixes before a diff round closes.
5. Ask the user only about `ESCALATE` items and open Blockers/Majors.
6. `operation: close` with `tokens` `{"reviews/goldfish_NN": token}`, `reports`, `findings`, and a
   `confirmation` for contested or escalated Blockers or Majors. A fixed Blocker (spec, planning) or
   Blocker or Major (design, diff) gives `reaudit-required`; `open` again: the one re-audit closes
   `accepted-residual` (needs `confirmation`) if it still reports one.
7. A PR merged without a diff round: open one at the PR head (`commit`), then record the merge
   with `merged_head`.
8. No fresh agent: `operation: waive` with `reason` and the user's `confirmation`; editorial
   edits may add `reuses` ([durability.md](durability.md)). Policy 3: see review-risk.md.
