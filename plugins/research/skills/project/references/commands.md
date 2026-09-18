# Routine project commands

Use the launcher pair resolved by `SKILL.md`. Paths below are absolute. Commands work on v3 and v4
unless stated otherwise. No MCP server or background service is needed.

## Compact state updates

`context <project-dir>` reports the current revision and a small working set; `--task T01` includes
one full task. `--limit 10` shows more active summaries (maximum 20). It does not load evidence,
old decisions, or completed task definitions unless that task is explicitly selected. Truncation
is reported, not silently treated as a complete specification.

Write a small JSON file and pass it to:

```sh
research-project update <project-dir> <patch.json> --expected-revision 0
```

Allowed project fields: `title`, `status`, `review`, `cancellation_reason`, `predecessor`, `tasks`.
Objects merge recursively; arrays and scalar values replace the supplied field; null remains null.
The `tasks` array is special: each entry updates the task with that ID or appends a new task.
Unmentioned tasks/fields remain unchanged. Tasks cannot be removed. Unknown fields are rejected.

For example, leave alignment and add a task:

```json
{
  "status": "PLANNING",
  "tasks": [{
    "id": "T01",
    "name": "Implement and verify the requested parser fix",
    "success_criteria": "Malformed input returns a finding without crashing",
    "verification": "Run the parser regression suite and repository lint/type checks",
    "outputs": [{"root": "target", "path": "src/parser.py", "required": true}],
    "effect": {"kind": "local_write", "description": "Edit the requested parser and its tests"}
  }]
}
```

New tasks require `id`, `name`, `success_criteria`, `verification`, and explicit `effect.kind`.
Declare outputs that matter to success; this example shows one. The tool supplies `TODO`, empty
dependencies/evidence/receipts/outputs, null block/skip reasons, and authorization defaults.
`none` effects need no description; all others do. `destructive` and `external` default to required,
pending authorization; the tool never invents consent. Other effects default to `not_required`.
Changing an existing effect does not silently change its authorization: update both explicitly.

Start work with a second patch at the returned revision:

```json
{"status": "EXECUTING", "tasks": [{"id": "T01", "status": "RUNNING"}]}
```

After work and passing verification, finish it and optionally start a planned successor:

```json
{
  "tasks": [
    {"id": "T01", "status": "DONE", "evidence": [
      {"root": "workspace", "path": "evidence.md", "anchor": "T01"}
    ]},
    {"id": "T02", "status": "RUNNING"}
  ]
}
```

`T02` must already have a complete definition. It can depend on `T01` because both changes commit
together. `current_tasks` is derived automatically. Block/skip reasons are not inferred: include
`block_reason` for `BLOCKED`, `skip_reason` for `SKIPPED`, and clear them with null when leaving
those states. Terminal task history, dependency, authorization, output and evidence checks are
the same as `commit`; the patch command constructs a candidate and uses that transaction.

Revision conflicts require reload and reconciliation. If index generation fails after the commit,
follow the reported `rebuild-index` recovery; the revision already landed. Use
`commit <project-dir> <candidate.json> --expected-revision R [--dry-run]` only when a complete
candidate is needed. Neither command changes execution protocol state.

## Evidence and authorization

```sh
research-project record-evidence <project-dir> --task T01 -- uv run pytest -q
```

Runs in `working_directory`, directly without a shell. Use an explicit shell for pipelines and
absolute paths for workspace files. The command records the actual exit status and output tail;
a failing run returns nonzero. `--tail-lines` bounds stored output. Verify that commands do not
print secrets before recording them. Evidence references name `root`, `path`, and `anchor` (or null).

For an authorized task, record the user's actual scope and source, for example:

```json
{"tasks": [{"id": "T03", "authorization": {
  "required": true, "status": "explicit",
  "scope": "Publish the approved guide to the specified staging site",
  "source": "User instruction in this session, 2026-09-18",
  "authorized_at": "2026-09-18T12:00:00+00:00"
}}]}
```

Use the real timestamp. Authorization must still apply when the action runs. Completed external
tasks need a receipt with `kind`, `value`, `destination`, `timestamp`. A value is an HTTP(S) URL or
a nonempty identifier prefixed by `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:`,
or `commit:`. Do not invent a receipt for an ordinary local change.

## Optional executor

Only use `run-auto <project-dir> [--concurrency N]` when worker execution is authorized and useful.
Default capacity is two. Admission requires v4, a clean Git target, `none`/`local_write` effects,
target-file outputs, explicit exhaustive `reads` (empty only for a self-contained task), separate
read-only `test`/`[` checks covering every required output, and an available Claude or Codex CLI.
Normal test-suite commands and unenumerable inputs do not fit this executor. Continue sequentially
in those cases; do not weaken verification to obtain admission.

Workers use isolated worktrees; the coordinator verifies and integrates accepted changes. Read
`execution/runtime/automatic-run.json`: `completed` is already evidenced and committed; `deferred`
can run in another wave; `fallbacks` gives a sequential handoff; `blocked` needs recovery without
replaying uncertain effects. Exit 2 means no task completed. Do not rerun structural refusals.
Nested coordination is prohibited. Never delete execution records/worktrees to clear a failure.

Existing v3 projects remain sequential unless explicitly activated with `enable-execution` and the
legacy-writer quiescence attestation. See `workspace-schema.md` for migration and
`parallel-execution.md` for protocol recovery.

## Optional paired report

The historical paired format has Markdown and HTML, five shared sections, and a task-graph
subsection. Its structural checker deliberately still requires both formats:

```sh
research-project record-evidence <project-dir> --step report -- \
  research-validate <project-dir> --report
```

Use the resolved validator launcher and absolute project path in the recorded command. `--step`
and `--task` are mutually exclusive; `report` is the only closure-step name. Plain deliverable
reports can use the user's preferred format and normal task verification instead.
