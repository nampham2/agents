# automation execution

## Worker observations and checks

`worker-event` uses the [common metadata fields](automation-records.md) plus `task`, `handle`,
`event`, `scope`, `observation`, `assignment_revision`. Events: `intent`, `launched`, `observed`,
`completed`, `stopped`, `failed`. It appends to `tasks/<id>.md`; supply the existing note's token.
Record actual host observations. Resume surfaces the latest unresolved observation per handle;
neither an intent record nor missing handle proves no worker exists. Use the host to observe/stop.

`verify` takes `id`, a nonempty `checks` array, optional boolean `continue_on_failure` (default
false), and optional selected `fingerprints` references. It needs no document token or state
revision; it does not change task status. Each check requires exactly one `task` or closure `step`
(currently `report`), an explicit nonempty `argv` array and optional positive `timeout` seconds
(default 300, maximum 3,600). Never translate arbitrary free-text `verification` into execution.

```json
{"id":"parser-checks-1","continue_on_failure":false,"checks":[
  {"task":"T01","argv":["python3","-m","unittest","discover"],"timeout":120}
],"fingerprints":[{"root":"target","path":"src/parser.py"}]}
```

Checks run without a shell in the target, using the evidence runner. Keep secrets out of argv and
output. Results distinguish recorded exits, rejected inputs, launch errors, timeouts and unknown
outcomes. No fabricated exit code is assigned to timeout or launch error. Only normal recorded
failures may continue the batch. A timeout does not prove descendant processes stopped.
Same-ID retries repair saved evidence without rerunning attempted commands; unknown outcomes stop
for inspection. Select a new ID for a genuinely new, authorized verification run. Zero exit alone
does not satisfy task acceptance.

## Staged lessons and recovery

`triage` takes `id` (maximum 45 characters), `expected_revision`, `tokens` for `memory-staging`
and, when receiving local lessons, `reflection`, plus `items`. Each item selects an exact,
independent level-two `section` in staging and supplies `disposition` (`promote`, `reflection`,
`discard`) and `reason`. Promotion additionally needs:

```json
{"topic":"parser-errors","body":"<agent-authored reusable lesson>",
 "expected_sha256":"missing","description":"Parser failure handling",
 "kind":"method","scope":"Parsers"}
```

Existing topics use their current token; new ones require description/kind/scope. Triage records
promotion source IDs, deduplicated incident identities and destination receipts before removing
selected staging sections. Unselected sections remain untouched; decisions remain in the journal.
For local reflection/discard, omit promotion fields. Topic-rule rewrites still use guarded
`compact-memory` after judgment; promotion appends an incident to an existing rule.

Composed writes preflight tokens, journal intended metadata, then save documents before the state
commit; final handoff is deliberately after DONE commits. This is recoverable, not cross-file
atomic. A failure reports saved steps and whether state landed. Inspect `recover {"id":"..."}`;
`apply:true` repairs metadata/index steps only. Checks and triage use their own same-ID command
for recovery. Reconcile source conflicts before proceeding. Never steal locks, clear ownership,
invent a successful result, or repeat an external action to repair bookkeeping.
