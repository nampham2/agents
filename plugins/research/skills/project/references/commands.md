# Routine commands

Use [named automation](automation.md) for composed work: `workflow <project-dir> <action> -`
accepts typed JSON on stdin. Follow its action-specific schemas and recovery contracts.
`init --json` supplies initial tokens; `update --dry-run` previews a small patch without writing.

## Retrieve

`context <project-dir> --validate` returns revision, active summaries, ready IDs, review/executor
status, roots, document tokens and validation findings. Specification text is capped at 6,000
characters; `--limit` sets summary count (1–20). `--task T01 --task-only` returns task/dependencies;
`--worker` changes its role, granting no authority.

`read <project-dir> spec --section "Constraints and important assumptions"` selects an exact
heading; `--outline` returns headings and the SHA-256 token. Default reads exclude Decision history;
select it explicitly. `read <project-dir> evidence --task T01` selects task entries; `--step report`
selects report-check evidence; omit both for all. `read <project-dir> architecture` or `handoff`
reads that document. Reads cap at 4,000 characters with `total_chars`, `truncated`, `next_offset`;
continue with `--offset <next_offset>`. `--max-chars` accepts 1–20,000. Reload changed sources.

`list-projects <root> --query "<text>" [--status EXECUTING]` searches identity/title/target,
returning 10 matches. Page with `--limit` (1–100) and `--offset`. Invalid/legacy records remain.

## Update

Send a small JSON patch:

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

Objects merge; lists/scalars replace. Tasks merge by ID; omissions stay unchanged.
Project fields: `title`, `status`, `review`, `cancellation_reason`, `predecessor`, `tasks`.
Include `block_reason` for `BLOCKED`, `skip_reason` for `SKIPPED`; clear them with null when
leaving. Terminal history is immutable. Use full `commit` only for changes outside these fields.
After a post-commit index failure, run the printed `rebuild-index` recovery; do not repeat the
update.

## Documents and records

Reuse tokens from edits, validated context or document/outline reads. Initial `--sections-json -`
maps the seven headings to bodies; later batches replace non-overlapping sections.
Reflection, architecture and handoff use whole-document `--body-file -`:

```sh
research-project edit <project-dir> spec --sections-json - --expected-sha256 <token>
research-project edit <project-dir> reflection --body-file - --expected-sha256 missing
research-project edit <project-dir> architecture --body-file - --expected-sha256 <token-or-missing>
```

`init --briefing` optionally seeds the grill.

Append prose:

```sh
research-project append <project-dir> decision --body-file -
research-project append <project-dir> finding --task T01 --body-file -
```

`--entry-id` makes retries idempotent; different content conflicts. Prose is not evidence.

## Evidence and effects

`record-evidence <project-dir> --task T01 --json -- <command>` records actual exit code and output
tail; failure returns nonzero. `--tail-lines` adjusts stored output; `--timeout <seconds>` abandons
a hung command. References have `root`, `path`, `anchor` (or null). Keep commands free of secrets.
Undecodable output bytes are stored as `\xNN` escapes.

`task ... start` resumes `EXECUTING` from `PLANNING`, `BLOCKED` or `REVIEW`, preserving review state
and normal dependency/authorization guards. Reopen invalidated review acceptance as `pending`.
Finish after judging success criteria:

```sh
research-project task <project-dir> finish T01 --evidence <record-id> \
  --start-next T02 --expected-revision N
```

`block` and `skip` require `--reason`. `read <project-dir> evidence --entries --task T01` lists
selectable checks; `--entry <record-id>` returns one complete entry. Legacy evidence stays valid but
is not selectable by generated ID.

Failed `close` reports whether reflection was saved and state committed. Keep saved drafts;
reload before retrying uncommitted state. For committed index/validation errors, use the reported
recovery, not another close. `workflow finalize` also saves the final handoff after DONE commits.

For destructive/external work, update authorization explicitly: `required: true`, `status:
explicit`, the actual `scope`, user-instruction `source`, and real `authorized_at` timestamp.
Changing an effect does not update authorization automatically. External completion also needs a
receipt with `kind`, `value`, `destination`, `timestamp`; value is an HTTP(S) URL or a nonempty
identifier prefixed by `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:`, or `commit:`.
