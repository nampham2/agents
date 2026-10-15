# The retro ledger

The ledger lives under the workspace root: one record per run in `retro/<id>.md` and the index
`RETRO.md`. `research-project retro record` is its only writer. It never changes a project.

## The record request

Send one JSON object on stdin:

```json
{"id": "2026-10-16-001",
 "source": "user reply 2026-10-16 via question tool",
 "batch": ["2026-10-15-003"],
 "candidates": [
  {"id": "c1", "title": "finalize names the missing section", "category": "gates", "severity": "high",
   "proposal": "Return the heading in the error. Add a test that shows it.",
   "evidence": [{"project": "2026-10-15-003", "record": "invocations.jsonl",
                 "anchor": "2026-10-15T16:02:11+02:00"},
                {"project": "2026-10-15-003", "record": "friction.md", "anchor": "entry-1a2b"}],
   "disposition": "accepted", "response": "yes, do that one"}],
 "notes": "optional"}
```

- `id`: use `next_id` from `retro batch`. Lowercase letters, digits and hyphens.
- `source`: where the dispositions came from, with the date.
- `batch`: the project ids of the rows you judged. Each must be closed. The tool computes
  `through`, the latest `updated` instant among them, as the cursor of the next run.
- `candidates`: may be empty for a run with no finding. Each needs `id` (unique in the run),
  `title` (one line, at most 120 characters), `category` and `severity` from
  [categories.md](categories.md), `proposal`, `evidence` (at least one pointer to an existing
  project), `disposition` (`accepted`, `rejected` or `deferred`) and `response` (the user's words).
- `notes`: optional free text.

`research-project retro record --schema` prints the keys.

## What the tool does

1. Refuses a request with an unknown key, an open or unknown project, a bad category, a candidate
   without evidence, or an evidence pointer to an unknown project. Nothing is written.
2. Writes `retro/<id>.md` under the ledger lock: a `research-retro-v1` JSON block, then the batch,
   the candidates by severity with their dispositions, and the next cursor.
3. Renders `RETRO.md` from every record on disk.
4. Returns the path, the counts per disposition, and `warnings`. A candidate whose title and
   category match a rejected one from an earlier run is written and warned about.

The same request again returns `existing: true`. The same id with other content is a conflict:
choose a new id.

## How the next run uses it

`retro batch` reads every record, takes the latest `through` as the cursor, skips the projects any
run covered, and returns `previous.rejected` and `previous.deferred`. A record the tool cannot parse
is listed under `previous.unreadable` and ignored.
