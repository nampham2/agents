# Relevant lessons

## Review at decision points

Assess lessons before requirements/design agreement, after direction changes, and at closure.
Reuse unchanged assessments; a checkpoint needs no new search.

`context` suggests up to three `memory_candidates`. For an uncovered consequential decision, search
the current workspace:

```sh
research-project search-memory "<query>" --workspace-root <root> --limit 5
```

Results have scores, excerpts and path/line pointers; `--verbose` adds scope/kind/status.
Page with `--offset`/`next_offset`. `--include-postmortems` adds substring matches;
`--include-retired` adds retired topics.

Open at most two promising topics first with `read-memory <slug> --workspace-root <root>`.
It returns rule, scope, status, sources and SHA-256; `--full` includes incidents. Legacy topics
(`tiered: false`) return whole bodies. Expand only for a named unresolved question. Avoid broad
index reads and other-root searches. Record no matches or unavailable retrieval once; empty
candidates can hide lookup failure.

## Persist and reuse applicability

Memory cannot override decisions or grant authority. Save a brief decision or task finding with
topic path/token, source projects, rule/conditions, applicability and disposition: applied
(concrete consequence), rejected (reason), or deferred (check and revisit point). Put adopted
consequences in spec/design/tasks. Prior evidence never verifies this project's outputs.

Handoffs and workers inherit only next-step assessments and pending checks. Reuse local summaries;
reopen shared sources for changed conditions or missing detail. If a source changes or disappears,
keep the summary and reassess affected claims; do not silently change agreed design. Only the
coordinator updates shared topics.

## Stage and resolve lessons

Stage reusable findings: `research-project stage <project-dir> --title <unique> --body-file -`
(conditions, lesson/hypothesis, evidence pointers; no headings). At closure, promote supported
findings, fold project-specific ones into reflection (`triage` `reflection` before finalize), or
discard with a reason. Record dispositions there; remove only resolved items. Report failed or
unauthorized promotion deferred. Staging warns unless memory delivery is required.

To promote, compact or retire topics, read [memory-promotion.md](memory-promotion.md).
