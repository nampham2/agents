# Routine commands

Use the resolved launchers and absolute paths. Commands support v3 and v4.

## Retrieve

`context <project-dir>` gives revision, active summaries, ready IDs, review/executor status and
up to 6,000 specification characters. `--limit` changes summary count (1–20).
`--task T01 --task-only` gives the full task and direct dependency references without rereading
specification or logs. `--worker` gives the same assignment with the worker role; the coordinator
supplies applicable constraints, decisions, and inputs. Neither view authorizes or starts work.
Legacy `--task T01` still adds a full task to the general resume view.

`read <project-dir> spec --section "Constraints and important assumptions"` selects an exact heading.
Without a section, it returns the current document before Decision history; that history can be
explicitly selected. `read <project-dir> evidence --task T01` retrieves that task's actual entries;
`--step report` selects report-check evidence. Omit the owner for all evidence.
Reads return up to 4,000 characters with `total_chars`, `truncated`, and `next_offset`.
Continue with `--offset <next_offset>`; `--max-chars` accepts 1–20,000. These are excerpts, not summaries.
File edits can change offsets; reload when the source changes.

`list-projects <root> --query "<text>" [--status EXECUTING]` searches identity, title and target.
It returns 10 matches by default; `--limit` (1–100) and `--offset` page results. Invalid and legacy
records remain visible for inspection even when filters cannot establish a match.

## Update

Write a small patch and run:

```sh
research-project update <project-dir> <patch.json> --expected-revision 0
```

```json
{"status":"PLANNING","tasks":[{
  "id":"T01","name":"Implement and verify parser fix",
  "success_criteria":"Malformed input produces a finding without crashing",
  "verification":"Run parser regressions and repository checks",
  "effect":{"kind":"local_write","description":"Edit parser and tests"},
  "outputs":[{"root":"target","path":"src/parser.py","required":true}]
}]}
```

New tasks require `id`, `name`, `success_criteria`, `verification`, and explicit `effect.kind`.
The tool defaults status to `TODO`, lists to empty, and block/skip reasons to null. Non-`none`
effects need a description. Destructive/external effects default to pending authorization.
Roots: `target` for repository outputs, `workspace` for project files, `workspace_root` for shared
records, `external` for remote outputs.

Start with `{"status":"EXECUTING","tasks":[{"id":"T01","status":"RUNNING"}]}`.
After passing checks, finish and optionally start a planned successor:

```json
{"tasks":[
  {"id":"T01","status":"DONE","evidence":[{"root":"workspace","path":"evidence.md","anchor":"T01"}]},
  {"id":"T02","status":"RUNNING"}
]}
```

Objects merge; lists/scalars replace. Task entries merge by ID; omitted fields/tasks stay unchanged.
Allowed project fields: `title`, `status`, `review`, `cancellation_reason`, `predecessor`, `tasks`.
Include `block_reason` for `BLOCKED`, `skip_reason` for `SKIPPED`; clear them with null when leaving.
Terminal history is immutable. Use full `commit` only for changes outside these fields.
After a post-commit index failure, run the printed `rebuild-index` recovery; do not repeat the update.

## Evidence and effects

`record-evidence <project-dir> --task T01 -- <command>` records actual exit code and output tail;
failure returns nonzero. `--tail-lines` adjusts stored output. References have `root`, `path`, `anchor`
(or null). Keep commands free of secrets. Do not rerun a check solely to duplicate existing evidence.

For destructive/external work, update authorization explicitly: `required: true`, `status: explicit`,
the actual `scope`, user-instruction `source`, and real `authorized_at` timestamp. Changing an effect
does not update authorization automatically. External completion also needs a receipt with `kind`,
`value`, `destination`, `timestamp`; value is an HTTP(S) URL or a nonempty identifier prefixed by
`receipt:`, `deployment:`, `message:`, `purchase:`, `publish:`, or `commit:`.
