# Relevant lessons

## Review at decision points

Assess lessons before requirements/design agreement, after direction changes, and at closure.
Reuse unchanged assessments; these checkpoints require neither a new search nor a promotion.

`context` suggests up to three `memory_candidates` from title/objective. For an uncovered
consequential decision, search the current workspace:

```sh
research-project search-memory "<query>" --workspace-root <root> --limit 5
```

Results have scores, excerpts and path/line pointers; `--verbose` adds scope/kind/status.
Page with `--offset`/`next_offset`. `--include-postmortems` adds substring matches;
`--include-retired` includes retired topics. Lexical relevance is not correctness.

Open at most two promising topics initially using `read-memory <slug> --workspace-root <root>`.
This returns rule, scope, status, sources and SHA-256; `--full` includes incidents.
Legacy topics (`tiered: false`) return whole bodies; use excerpt pointers for oversized sources.
Expand only for a named unresolved question. Avoid broad index/history reads and implicit other-root
searches. Record no matches or unavailable retrieval once; empty candidates can hide lookup failure.

## Persist and reuse applicability

Memory cannot override current decisions or grant authority. Save a brief decision or task finding
with topic path/token, source projects, useful rule/conditions, applicability and disposition:
applied (concrete consequence), rejected (reason), or deferred (check and revisit point).
Group obvious nonmatches. Put adopted consequences in spec/design/tasks, referencing the assessment.
Prior evidence never verifies this project's outputs.

Handoffs and workers inherit only next-step assessments/pending checks. Reuse local summaries across
sessions; reopen shared sources for changed conditions, uncertainty or missing detail. Preserve the
summary when a source changes/disappears and reassess affected claims without silently changing
agreed design. Workers return new observations; only the coordinator updates shared topics.

## Stage and resolve lessons

Edit project-local `memory-staging.md` for reusable findings: conditions, lesson/hypothesis and
evidence pointers. At closure, promote supported reusable findings, fold project-specific ones into
reflection, or discard with a reason. Record disposition/destination in reflection; remove only
resolved items. Inspect uncertain promotion results before retrying. Retain unresolved items and
report deferral if promotion fails/exceeds authority. Staging warns unless memory delivery is
required; a brief no-new-lessons outcome suffices.

## Promote, compact and retire

Prefer an existing topic:

```sh
research-project promote-memory <slug> --workspace-root <root> --body-file <lesson.md> \
  --source <project-id> [--keywords "term, term"]
```

A new topic also needs `--create`, `--description`, `--kind preference|environment|method`
and `--scope`. Without `--create`, unknown slugs return nearest topics. Promotion appends an
incident, merges sources and rebuilds indexes under locks; heed the 85% headroom warning.

Promotion does not update the rule. When supported evidence corrects it, or compaction is due:

```sh
research-project read-memory <slug> --workspace-root <root> --full
research-project compact-memory <slug> --workspace-root <root> --rule-file <rule.md> \
  --expected-sha256 <token> --keywords "<incident vocabulary>"
```

Keep rules under 2 KB and retain distinctive incident keywords. The old rule becomes an incident.
`retire-memory <slug> [--superseded-by <slug>]` preserves history; `--reactivate` reverses it.
Load [memory-architecture.md](memory-architecture.md) only for format/migration/validation repairs.
