# Routine commands

Use [named automation](automation.md) for composed work: `workflow <project-dir> <action> -` takes
typed JSON on stdin. `init --json` supplies initial tokens; `update --dry-run` previews a patch.

## Retrieve

`context <project-dir>` returns revision, roots and the `tokens` map that guarded writes take.
`--validate` adds active summaries, ready IDs, review/executor status and validation findings.
`--task T01 --task-only` returns task/dependencies; `--worker` changes its role, granting no
authority.

`read <project-dir> spec --section "<exact heading>"` selects an exact heading; `--outline` returns
headings and the SHA-256 token. Default reads exclude Decision history; select it explicitly.
`read <project-dir> evidence --task T01` selects task entries; `--step report` selects report-check
evidence; omit both for all. `read <project-dir> planning` or `handoff` reads that document. Reads
cap at 4,000 characters; continue with `--offset <next_offset>`.

`list-projects <root> --query "<text>" [--status EXECUTING]` matches every word in
identity/title/target.

## Update

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

New tasks require `id`, `name`, `success_criteria`, `verification` and `effect.kind`. Non-`none`
effects need a description. Destructive/external effects default to pending authorization.
Roots: `target` for repository outputs, `workspace` for project files, `workspace_root` for shared
records, `external` for remote outputs.

Objects merge; lists/scalars replace. Tasks merge by ID; omissions stay unchanged.
Project fields: `title`, `status`, `review`, `cancellation_reason`, `predecessor`, `tasks`.
Include `block_reason` for `BLOCKED`, `skip_reason` for `SKIPPED`; clear them with null when
leaving. Use full `commit` only for other fields.

## Documents and records

Reuse tokens from edits, context or outline reads. Initial `--sections-json -`
maps the seven headings to bodies; later batches replace non-overlapping sections.
Reflection, planning and handoff use whole-document `--body-file -`:

```sh
research-project edit <project-dir> spec --sections-json - --expected-sha256 <token>
research-project edit <project-dir> reflection --body-file - --expected-sha256 missing
research-project edit <project-dir> planning --body-file - --expected-sha256 <token-or-missing>
```

Append prose:

```sh
research-project append <project-dir> decision --body-file -
research-project append <project-dir> finding --task T01 --body-file -
research-project append <project-dir> friction --kind gate-refusal --pointer <ref> --body-file -
```

Append friction when an instruction, reference or gate got in the way. `--entry-id` makes retries
idempotent; different content conflicts.

## Evidence and effects

`record-evidence <project-dir> --task T01 --json -- <command>` records actual exit code and output
tail; failure returns nonzero. `--tail-lines` adjusts stored output; `--timeout <seconds>` abandons
a hung command; `--cwd <absolute-dir>` runs elsewhere; `--dry-run` runs nothing.
The command sees `$RESEARCH_PROJECT_DIR`; relative paths to `research-*` tools are
refused. Warnings (task not `RUNNING`, heredoc, `$(` in `bash -c`) never block; an unguarded
`bash -c` pipe is refused unless `set -o pipefail` or a script file. `pre-commit` rewrites record
`Outcome: rewrite`.

In zsh, `GID` and `path` are protected names and `$var` does not word-split; use arrays. BSD
`sed -i ''` needs the empty suffix; `grep -e` guards patterns that start with a dash.

`task ... start` resumes `EXECUTING` from `PLANNING`, `BLOCKED` or `REVIEW`, preserving review state
and dependency/authorization guards. Reopen invalidated review acceptance as `pending`.
Finish after judging success criteria:

```sh
research-project task <project-dir> finish T01 --evidence <id> <id> \
  --start-next T02 --expected-revision N
```

`block` and `skip` require `--reason`; `finish --backfill --note <why>` is start plus finish (two
commits) for work done before `start`. `read <project-dir> evidence --entries --task T01` lists
checks; `--entry <record-id>` returns it.

A failed `close` reports what was saved; keep drafts, reload before retrying, and use reported
recovery, never another close. `workflow finalize` also
saves the handoff.

External effects: `workflow authorize` (`status: explicit`, actual `scope`, user `source`, real
`authorized_at`; an effect change does not update it), `task start`, the effect through
`record-evidence`, `workflow receipt` ([formats](automation-records.md)), then `finish`.
