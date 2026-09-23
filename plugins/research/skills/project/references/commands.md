# Routine commands

Commands support v3 and v4.

## Retrieve

`context <project-dir> --validate` gives revision, active summaries, ready IDs, review/executor
status, roots, document tokens and full validation findings in one call. Without `--validate`,
context gives up to 6,000 specification characters. `--limit` changes summary count (1–20).
`--task T01 --task-only` gives the full task and direct dependency references without rereading
specification or logs. `--worker` gives the same assignment with the worker role; the coordinator
supplies applicable constraints, decisions, and inputs. Neither view authorizes or starts work.

`read <project-dir> spec --section "Constraints and important assumptions"` selects an exact
heading. `read <project-dir> spec --outline` returns headings and the whole-document SHA-256 token.
Without a section, it returns the current document before Decision history; that history can be
explicitly selected. `read <project-dir> evidence --task T01` retrieves that task's actual entries;
`--step report` selects report-check evidence. Omit the owner for all evidence. `read <project-dir>
architecture` returns the design document whole. Reads return up to 4,000 characters with
`total_chars`, `truncated`, and `next_offset`. Continue with `--offset <next_offset>`; `--max-chars`
accepts 1–20,000. File edits can change offsets; reload when the source changes.

`list-projects <root> --query "<text>" [--status EXECUTING]` searches identity, title and target.
It returns 10 matches by default; `--limit` (1–100) and `--offset` page results. Invalid and legacy
records stay visible.

## Update

Send a small patch through stdin; a file path remains supported:

```sh
research-project update <project-dir> - --expected-revision 0 --json
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

Objects merge; lists/scalars replace. Task entries merge by ID; omitted fields/tasks stay unchanged.
Allowed project fields: `title`, `status`, `review`, `cancellation_reason`, `predecessor`, `tasks`.
Include `block_reason` for `BLOCKED`, `skip_reason` for `SKIPPED`; clear them with null when
leaving. Terminal history is immutable. Use full `commit` only for changes outside these fields.
After a post-commit index failure, run the printed `rebuild-index` recovery; do not repeat the
update.

## Documents and records

Use the SHA-256 from `read ... spec --outline` to prevent overwriting a concurrent edit. Initial
`--sections-json -` input maps all seven exact specification headings to body strings; later batches
may replace any unique non-overlapping sections. Reflection and architecture are replaced whole with
`--body-file -`:

```sh
research-project edit <project-dir> spec --sections-json - --expected-sha256 <token>
research-project edit <project-dir> reflection --body-file - --expected-sha256 missing
research-project edit <project-dir> architecture --body-file - --expected-sha256 <token-or-missing>
```

`init --briefing` scaffolds an optional `briefing.md` that seeds the grill.

Append prose without rewriting Markdown:

```sh
research-project append <project-dir> decision --body-file -
research-project append <project-dir> finding --task T01 --body-file -
```

Supply `--entry-id` for retry-safe appends. The same ID and body returns the existing entry;
different content conflicts. Decisions and findings are not command evidence.

## Evidence and effects

`record-evidence <project-dir> --task T01 --json -- <command>` records actual exit code and output
tail; failure returns nonzero. `--tail-lines` adjusts stored output; `--timeout <seconds>` abandons
a hung command. References have `root`, `path`, `anchor` (or null). Keep commands free of secrets.
Undecodable output bytes are stored as `\xNN` escapes.

Start work with `task <project-dir> start T01 --expected-revision N`; its JSON includes the complete
task, direct dependencies and roots. Finish only after judging success criteria:

```sh
research-project task <project-dir> finish T01 --evidence <record-id> \
  --start-next T02 --expected-revision N
```

`block` and `skip` require `--reason`. `read <project-dir> evidence --entries --task T01` lists
selectable checks; `--entry <record-id>` returns one complete entry. Legacy evidence stays valid but
is not selectable by generated ID.

Close with an existing reflection token or supply one through `--reflection-file -`:

```sh
research-project close <project-dir> --expected-revision N \
  --reflection-file - --expected-reflection-sha256 <token-or-missing>
```

If the reflection is saved but the state commit is rejected, the failing JSON result reports
`reflection_saved: true`, `committed: false`, the new reflection token, and actual state revision.
Keep that draft, reload context, and retry against the reported state; do not rewrite the
reflection. If `committed: true` accompanies an index or final-validation error, do not retry
`close`; run the reported `rebuild-index` and closure-validation recovery instead.

For destructive/external work, update authorization explicitly: `required: true`, `status:
explicit`, the actual `scope`, user-instruction `source`, and real `authorized_at` timestamp.
Changing an effect does not update authorization automatically. External completion also needs a
receipt with `kind`, `value`, `destination`, `timestamp`; value is an HTTP(S) URL or a nonempty
identifier prefixed by `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:`, or `commit:`.
