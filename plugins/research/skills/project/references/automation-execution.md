# automation execution

## Worker observations and checks

`record-observation` is not a workflow action. For a check with no command (an MCP read, a query) it
records `--source`, `--result passed|FAILED` and the observed text (`--body` or `--body-file -`)
under a task or closure step. The entry has an `Observation:` line and no exit code, so `task finish
--evidence` refuses it; `--observation` accepts it. A finish resting only on observations appends an
attested-finish note; a `FAILED` one never finishes the task.

`worker-event` uses the [common metadata fields](automation.md) plus `task`, `handle`, `event`,
`scope`, `observation`, `assignment_revision`. Events: `intent`, `launched`, `observed`,
`completed`, `stopped`, `failed`. It appends to `tasks/<id>.md`; supply the note's token. Record
actual host observations. Resume surfaces the latest unresolved observation per handle; neither an
intent record nor a missing handle proves no worker exists.

`verify` takes `id`, a nonempty `checks` array, optional boolean `continue_on_failure` (default
false), optional check-shaped `control`, and optional `fingerprints` references. It needs no token
or revision and leaves task status alone. Each check requires exactly one `task` or closure `step`
(currently `report`), a nonempty `argv` array and optional positive `timeout` seconds (default 300,
maximum 3,600). Never translate free-text `verification` into execution.

```json
{"id":"parser-checks-1","continue_on_failure":false,"checks":[
  {"task":"T01","argv":["python3","-m","unittest","discover"],"timeout":120}
],"control":{"task":"T01","argv":["python3","-c","raise SystemExit(1)"]},
 "fingerprints":[{"root":"target","path":"src/parser.py"}]}
```

Checks run without a shell in the target, using the evidence runner. Keep secrets out of argv.
Results distinguish recorded exits, rejected inputs, launch errors, timeouts and unknown outcomes;
the last three get no exit code. Only recorded failures continue the batch. A timeout does not prove
descendants stopped. Same-ID retries repair saved evidence without rerunning; unknown outcomes stop
for inspection. A new run needs a new ID. Zero exit alone does not satisfy acceptance.

The `control` is a check declared to fail on the current content; it runs last and validates in the
same pre-pass; a malformed batch records nothing. The response carries `controls_fired` as `n/m`;
the control fires only on a recorded non-zero exit, so timeout, launch error and rejection report
`0/1` and fail the batch. Without a control it reports `0/0` and passes when every check records
zero. A control recording zero proves nothing and fails the batch. Give each acceptance batch a
control the content fails; `finalize` warns when verify batches exist but no control was ever
observed failing.

## Staged lessons and recovery

`triage` takes `id` (maximum 45 characters), `expected_revision`, `tokens` for `memory-staging` and,
for local lessons, `reflection`, plus `items`. Each item selects an independent level-two `section`
in staging with `disposition` (`promote`, `reflection`, `discard`) and `reason`. Promotion also
needs:

```json
{"topic":"parser-errors","body":"<agent-authored reusable lesson>",
 "expected_sha256":"missing","description":"Parser failure handling",
 "kind":"method","scope":"Parsers"}
```

Existing topics use their token; new ones require description/kind/scope. Triage records source IDs,
deduplicated incident identities and destination receipts before removing selected sections. A body
the topic already holds under another entry is skipped, listed in `skipped_duplicates`. For
reflection/discard, omit promotion fields. Topic-rule rewrites use guarded `compact-memory`;
promotion appends an incident to an existing rule.

Composed writes preflight tokens, journal metadata, save documents, then commit state; the final
handoff follows the DONE commit. Not cross-file atomic: a failure reports saved steps and whether
state landed. Inspect `recover {"id":"..."}`; `apply:true` repairs metadata/index steps only. Checks
and triage recover through their own same-ID command. Reconcile source conflicts first. Never steal
locks, clear ownership, invent success, or repeat an effect to repair bookkeeping.
