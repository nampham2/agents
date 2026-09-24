# Lifecycle automation

Use the launchers resolved beside the loaded skill (Codex) or on PATH (Claude). Examples use
`research-project`; substitute the resolved executable, never a Python implementation module.

```sh
research-project workflow <project-dir> <action> -
```

Send one JSON object on stdin (`-`), or supply a JSON file path. Unknown fields fail.
Use named commands for bookkeeping; keep decisions, actual confirmations, chosen checks and
acceptance agent-authored. These helpers do not schedule agents or expand permission.

Read only the applicable command reference:

- [Context](automation-context.md): `resume`, `packet`, conditional/exact `read`, `freshness`,
  `preview`, `impact`, `fingerprint`, `readiness`, `memory-health`, and `recover`.
- [Records](automation-records.md): `checkpoint`, `round`, `confirm`, `correct`, `reconcile`,
  `authorize`, `receipt`, `review`, `finalize`, `maintenance`, `cancel`, `assess`, and `report`.
- [Execution and memory](automation-execution.md): native `worker-event` observations,
  durable `verify` check batches, and retry-safe staged-lesson `triage`.

Prefer `init ... --json` for initial paths/tokens and `update ... --dry-run` for patch previews.
Writes return tokens, revisions and partial outcomes. Retain exact operation input for retries.
Use a new ID for new work, not to bypass a conflict. Same-ID recovery repairs bookkeeping;
unknown command outcomes require inspection, never blind replay. A saved draft or passing check
does not establish user agreement, effect authorization, task acceptance or stopped workers.
