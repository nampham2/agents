# Optional execution accounting

Use this only for a requested account of task execution or legacy strict report checks.
Add a written `### Task graph` subsection under `## What was done` (`<h3>` under `<h2>` in HTML).
The validator accepts equivalent graph headings and deeper subsection levels.

Use `research-project show-graph <project-dir>` as the source. Include one row per task with
ID/name, dependency level, dependencies, status, effect, authorization, recorded evidence pass/fail
counts, and measured span or `unmeasured`. Explain consequential dependencies and execution changes.
Label the revision or snapshot time; a report task may still be RUNNING at that snapshot.

Spans cover first-to-last recorded verification timestamps, not total task duration. Unmeasured
work is not zero-duration work. Multiple report formats must agree. Charts are optional; HTML
guidance is in [report-html.md](report-html.md).

Validate with `--report-format markdown|html|both --report-profile execution`. Omitting the profile
retains this historical contract; bare `--report` checks both formats.
