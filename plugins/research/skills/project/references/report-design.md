# Report design

Read this reference only when a report is requested. Markdown and HTML are independent deliverables:
write `artifacts/report.md` for Markdown, `artifacts/report.html` for HTML, or both when both are
requested. Read [report-html.md](report-html.md) only for HTML output; it contains the CSS, theme, chart, and geometry
guidance. No report is required to close a project.

## What the report is for

The report is for a reader outside the session who wants to know what was done, what it found, and
what it did not settle. Cite existing evidence rather than introducing new measurements. Every figure
traces to `evidence.md`, an `artifacts/` file, or a receipt. State what was not proven and distinguish
mechanical checks from observations.

For a cancelled project, the summary states the cancellation reason and Open work describes what a
successor would pick up. Do not present cancellation as successful completion.

Markdown is the plain technical record, readable in a terminal and suitable for diffs. HTML presents
the same kind of findings with optional charts. When both are requested, they must agree on figures,
verdicts, and limitations; reuse shared content when helpful. The plugin ships no converter.

## The section contract

Each requested file carries these five sections, at `##` in Markdown and `<h2>` in HTML:

```text
## Summary
## What was done
## Findings and evidence
## Limitations and what was not proven
## Open work
```

The validator recognises reworded headings such as Abstract, Method, Results, Caveats, and Next steps.
A section must be present and written, and cannot be demoted to `###` or `<h3>`.

- **Summary** — the verdict in a few sentences, including the qualification a reader needs first.
- **What was done** — method and scope, enough to judge whether the finding transfers to another case.
  Task ids can support the explanation; a task list alone is not a method.
- **Findings and evidence** — each claim beside its evidence, cited by file and anchor.
- **Limitations and what was not proven** — the boundary of the claim, including checks the
  environment prevented and what fixtures establish compared with real runs.
- **Open work** — what a successor picks up and where any follow-up work is already recorded.

## Technical style

Use concise, readable sentences. Use bullets for parallel items, tables for exact values, and charts
when comparisons benefit from a picture. Give numbers their units and basis, and explain what went
wrong when it matters to the method. Avoid decorative emoji, heading repetition, and padding.

## The task-graph subsection

Each requested report carries a written subsection under What was done:

```text
### Task graph
```

In HTML use `<h3>`. Deeper headings are accepted too. Recognised alternatives include The task graph,
Dependency graph, Task dependency graph, and Plan and execution graph. The subsection must sit under
What was done; the same heading under another section does not satisfy the contract.

Use `research-project show-graph <project-directory>` as the source. Include a table with one row per
task: id and name, dependency level, dependencies, status at the report check, effect kind, authorization status,
recorded evidence pass/fail counts, and measured span or `unmeasured`. Explain how dependencies shaped
the work and what execution changed. Markdown needs the table and prose; HTML chart guidance is in
`report-html.md`.

Label the table's revision or snapshot time. A report task can still be RUNNING while its report is
being checked; say so instead of predicting completion. The canonical task state records later acceptance.

A span measures recorded verification, not total work: it runs from the task's first to last readable
`- Recorded:` evidence stamp and is a lower bound. Tasks without recorded commands remain in the table
as `unmeasured`, never as zero duration. If both formats are requested, their task tables must agree.

## Validation and evidence

Select the requested output explicitly:

```sh
research-project record-evidence <project-dir> --step report -- \
  research-validate <project-dir> --report-format markdown
```

Use `--report-format html` or `--report-format both` for those requests. The selected files must exist
and be readable, with all five sections and the task-graph subsection written. HTML selection also
checks tag balance, resource loading, colour tokens, theme blocks, and chart accessibility against
`report-html.md`. An unselected counterpart is not required or mechanically checked by this option.
Bare `--report` keeps the legacy requirement for both files; it cannot be combined with
`--report-format`.

The check cannot establish whether findings are true, figures trace to evidence, charts are readable
or geometrically accurate, two requested formats agree, or limitations are honest. Verify those
properties separately and state what was not observed.

At closure, and for a project already DONE or CANCELLED, existing report files receive warnings for
unreadable content, unwritten or missing sections, and a missing task-graph subsection. Each format is
independently optional: a missing counterpart produces no warning. These warnings do not prevent
closure or reopening. Reports are not checked before closure unless explicitly requested.
