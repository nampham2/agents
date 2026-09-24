# Relevant lessons

## Review at decision points

Review lessons before requirements confirmation and architecture agreement; reuse assessments
covering unchanged decisions. After a direction change, revisit affected lessons and uncovered
risks. A surprising failure may justify another lookup. Resolve staging at closure. These are agent
checkpoints, not schema gates; reviewing does not require a fresh search or promotion every time.

Use the current workspace root. `context` suggests up to three `memory_candidates` from title and
objective; a suggestion is not adoption. Search only for an uncovered consequential decision:

```sh
research-project search-memory "<query>" --workspace-root <root> --limit 5
```

Results include topic, score, description, excerpt and path/line;
`--verbose` adds scope/kind/status.
Page with `--offset`/`next_offset`. `--include-postmortems` adds substring matches;
`--include-retired` includes retired topics. Scores measure lexical relevance, not correctness.

Start with candidates or one query, opening at most two promising topics initially through
`read-memory <slug> --workspace-root <root>`. It returns the rule, scope, status, sources and
`sha256`. Expand only for a named unresolved question; use `--full` only for needed incidents.
Legacy topics return their whole body (`tiered: false`); prefer excerpt pointers for targeted
reads of oversized material. Do not preload indexes, all topics or postmortems, or search other
roots implicitly. Stop once the next decision has enough support; there is no lesson quota.

Record absent memory or no useful matches once, with the search scope when applicable. Empty
context candidates can hide lookup failures; distinguish unavailable retrieval from no matches.
Continue independent work, disclosing assumptions instead of inventing a lesson.

## Decide and persist applicability

Check scope, environment, sources and current requirements. Memory cannot override user decisions
or grant authority. Material changes still require the normal requirements/design agreement.

Save a concise `append ... decision`, or task finding for local choices: date/checkpoint, query or
candidate source, topic path/token, source-project IDs, relevant rule/conditions and disposition:

| Disposition | Record |
| --- | --- |
| Applied | Why it fits and the concrete requirement, design choice, task instruction or acceptance check it changes. |
| Rejected | Why it does not apply or conflicts with stronger/current evidence. |
| Deferred | What must be checked or decided, and when to revisit it. |

Use a few sentences per relevant lesson; group obvious nonmatches. Preserve its useful rule and
scope locally, not just a mutable link. Put adopted consequences in spec/design/tasks, referencing
the assessment rather than repeating it. For example, a migration lesson can become a criterion
and regression check. Prior-project evidence does not verify this project's implementation.

## Carry lessons through handoff and resume

`handoff.md` carries only next-step lesson summaries/assessment pointers and pending checks or
staging, or a none/unavailable outcome. The successor reads the project-local assessment even if
its source disappears from candidate rankings. Reuse it by default; do not reread every shared
source or repeat searches solely because the session changed.

Reopen a source when applicability is uncertain, the next decision needs more detail, or changed
conditions/advice warrant it. Compare status/token and relevant rule; an added incident alone need
not invalidate adoption. If retired, changed or missing, preserve the local summary and reassess
affected claims. Never silently replace agreed design with a changed shared rule.

Give workers only relevant adopted lessons/pending checks; task-only context omits memory. Workers
return observations to the coordinator, which alone updates shared topics.

## Stage and resolve lessons

Stage findings that could change future projects in project-local `memory-staging.md`: conditions,
lesson/hypothesis, evidence/finding pointers and related topic. The coordinator edits this file;
no staging command is needed. Avoid empty files, secrets and transcripts.

Before closure, assess staged items and useful/misleading adopted lessons. Promote supported,
reusable findings, fold project-specific ones into `reflection.md`, or discard with a reason.
Record disposition/destination in reflection and remove only successfully resolved staging items.
After uncertain promotion results, inspect the topic/source incident before retrying.

Retain unresolved items and report deferral when promotion fails or exceeds authority. Staging
remains a warning unless memory delivery is explicitly required. Record a no-new-lessons outcome
briefly when appropriate; the closure assessment is required, promotion is conditional.

## Promote

Promote a lesson only when it changes future work. Prefer amending an existing topic:

```sh
research-project promote-memory <slug> --workspace-root <root> --body-file <lesson.md> \
  --source <project-id> [--keywords "term, term"]
```

A new slug also needs `--description`, `--kind preference|environment|method`, `--scope`, and
`--create`. Without `--create` the tool refuses and lists the nearest existing topics; amend one of
them, or rerun with `--create` when the lesson is genuinely new. Every promotion appends a dated
incident entry, merges sources, rebuilds the indexes under locks, and prints index headroom; heed
the 85% warning by merging or retiring rather than by appending.

## Compact and retire

If supported new evidence corrects the current rule, compact after promotion even without a size
warning; appending an incident alone does not update the rule later readers receive.

When validation or a promotion warns that a topic is due for compaction, write its rule:

```sh
research-project read-memory <slug> --workspace-root <root> --full
research-project compact-memory <slug> --workspace-root <root> --rule-file <rule.md> \
  --expected-sha256 <token> --keywords "<incident vocabulary>"
```

The previous rule, or a legacy body, becomes the newest incident; nothing is discarded. Keep the
rule under 2 KB and carry the incidents' distinctive terms into `--keywords` so the topic stays
retrievable. Retire a topic that no longer applies with `retire-memory <slug> [--superseded-by
<slug>]`; `--reactivate` reverses it. Retirement never deletes.

Read [memory-architecture.md](memory-architecture.md) only for format changes, migration, validation
repairs or implementation work. Routine search, promotion and compaction do not need that history.
