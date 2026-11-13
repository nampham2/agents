# Routine commands

Composed work: [named automation](automation.md), `workflow <project-dir> <action> -` with JSON on
stdin.

## Retrieve

`context <project-dir>` returns revision, roots and the guarded-write `tokens` map. `--validate`
adds active summaries, ready IDs, review/executor status, findings. `--compact` returns the project
summary in one object; no other projection. `--task T01 --task-only` returns task/dependencies;
`--worker` sets the role, no authority.

`read <project-dir> spec --section "<exact heading>"` selects a heading; `--outline` returns
headings and the SHA-256 token. Default reads exclude Decision history. `read <project-dir> evidence
--task T01` selects task entries; `--step report` report checks. `read <project-dir> planning` or
`handoff` reads that document. Reads cap at 4,000 characters; continue with `--offset
<next_offset>`.

`list-projects <root> --query "<text>" [--status EXECUTING]` matches every word in
identity/title/target.

## Update

```sh
research-project update <project-dir> - --expected-revision 0 --json
```

```json
{"status":"PLANNING","tasks":[{
  "id":"T01","name":"Fix parser",
  "success_criteria":"Malformed input yields a finding, no crash",
  "verification":"Run parser tests",
  "effect":{"kind":"local_write","description":"Edit parser"},
  "outputs":[{"root":"target","path":"src/parser.py","required":true}]
}]}
```

New tasks require `id`, `name`, `success_criteria`, `verification` and `effect.kind`. Non-`none`
effects need a description; destructive/external effects start pending authorization. Roots:
`target` (repository outputs), `workspace` (project files), `workspace_root` (shared records),
`external` (remote outputs).

Objects merge; lists/scalars replace. Tasks merge by ID. Project fields: `title`, `status`,
`review`, `cancellation_reason`, `predecessor`, `tasks`. Include `block_reason` for `BLOCKED`,
`skip_reason` for `SKIPPED`; null them when leaving. Use `commit` only for other fields.

## Documents and records

Initial `--sections-json -` maps the seven headings to bodies; later batches replace non-overlapping
sections. Reflection, planning and handoff use `--body-file -`:

```sh
research-project edit <project-dir> spec --sections-json - --expected-sha256 <token>
research-project edit <project-dir> reflection --body-file - --expected-sha256 missing
research-project edit <project-dir> planning --body-file - --expected-sha256 <token-or-missing>
```

Append:

```sh
research-project append <project-dir> decision --body-file -
research-project append <project-dir> finding --task T01 --body-file -
research-project append <project-dir> friction --kind gate-refusal --pointer <ref> --body-file -
```

Append friction when an instruction, reference or gate got in the way. `--entry-id` makes retries
idempotent.

## Evidence and effects

`record-evidence <project-dir> --task T01 --json -- <command>` records exit code and output tail;
failure returns nonzero. `--timeout <seconds>` abandons a hung command; `--cwd <absolute-dir>` runs
elsewhere; `--dry-run` only warns. The command sees `$RESEARCH_PROJECT_DIR`; relative paths to
`research-*` tools are refused. Warnings (task not `RUNNING`, heredoc, `$(` in `bash -c`) never
block; an unguarded `bash -c` pipe is refused unless `set -o pipefail` or a script file. A
`pre-commit` rewrite records `Outcome: rewrite` and stays failed; rerun it.

zsh: `GID` and `path` are protected names and `$var` does not word-split; use arrays. BSD
`sed -i ''` needs the empty suffix; `grep -e` guards patterns starting with a dash.

`task ... start` resumes `EXECUTING` from `PLANNING`, `BLOCKED` or `REVIEW`, keeping review state
and guards. Reopen invalidated review acceptance as `pending`. Finish after judging success
criteria:

```sh
research-project task <project-dir> finish T01 --evidence <id> <id> \
  --start-next T02 --expected-revision N
```

`block` and `skip` require `--reason`; `finish --backfill --note <why>` is start plus finish for
earlier work. `read <project-dir> evidence --entries --task T01` lists checks; `--entry <record-id>`
returns one.

A failed `close` reports what was saved; keep drafts, reload, use the reported recovery
(`rebuild-index` after an index failure); never repeat it. Layout, `doctor`:
[records-layout](records-layout.md).

External effects: `workflow authorize` (`status: explicit`, actual `scope`, user `source`, real
`authorized_at`; an effect change does not update it), `task start`, the effect through
`record-evidence`, `workflow receipt` ([formats](automation.md)), then `finish`.
