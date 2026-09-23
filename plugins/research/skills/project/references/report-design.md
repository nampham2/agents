# Requested reports

Reports are optional deliverables. Default to Markdown; add HTML only when requested.
Use `artifacts/report.md` and/or `artifacts/report.html` for the built-in checks. Plain deliverables
elsewhere can use the user's format and normal task verification. Read
[report-html.md](report-html.md) only for HTML.

Write for the reader's question: outcome, relevant findings, evidence, limitations, and next steps.
Cite existing evidence rather than repeating logs or introducing unmeasured claims. A cancelled
project states its cancellation and unfinished work. Multiple requested formats must agree.

The concise built-in profile uses five written sections (`##` in Markdown, `<h2>` in HTML):

```text
## Summary
## What was done
## Findings and evidence
## Limitations and what was not proven
## Open work
```

Reworded headings such as Abstract, Method, Results, Caveats, and Next steps are accepted.
A short paragraph per section can suffice. Detailed task tables, graphs and charts are optional.

Record the selected format's check:

```sh
research-project record-evidence <project-dir> --step report -- \
  research-validate <project-dir> --report-format markdown --report-profile concise
```

Use the resolved validator launcher and absolute paths. `--task` and `--step` are mutually
exclusive; `report` is the only closure-step name. Use `html` or `both` when requested. HTML also
checks tag balance, resource loading, colour tokens, themes and chart accessibility. Structural
checks cannot establish truth, visual quality, or whether the evidence supports the claims.

For requested execution accounting, read [report-execution.md](report-execution.md) and select
`--report-profile execution`. Omitting the profile retains legacy strict checks; bare `--report`
still requires both formats and the task-graph subsection. Normal closure warns about unreadable
or incomplete report sections, but does not require optional graphs or missing counterparts.
