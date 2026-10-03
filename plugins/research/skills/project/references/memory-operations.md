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

Stage reusable findings: `research-project stage <project-dir> --title <unique> --body-file -`
(conditions, lesson/hypothesis, evidence pointers; no headings). At closure, promote supported
reusable findings, fold project-specific ones into reflection, or discard with a reason. Record
disposition/destination in reflection; remove only resolved items. A retried promotion adds nothing
already recorded. Retain unresolved items and report deferral if promotion fails/exceeds authority.
Staging warns unless memory delivery is required; a brief no-new-lessons outcome suffices.

To promote, compact or retire a topic, read [memory-promotion.md](memory-promotion.md).
