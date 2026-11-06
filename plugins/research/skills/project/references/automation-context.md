# automation context

## Reads and preparation

Prefer `init ... --json` for path, revision, roots, tokens and warnings. `update ... --dry-run
--expected-revision N` previews a patch: changed fields and validation findings.

| Action | Input fields and result |
| --- | --- |
| `resume` | Optional `task`, `selections`, `max_chars`; validation, context, source slices, freshness, memory health, worker observations. |
| `packet` | Like resume, plus optional `worker`, `scope`, `lessons` from the coordinator. Neither dispatches nor authorizes. |
| `read` | Required `document`; optional `section` or `entry`, `offset`, `max_chars`, `if_sha256`. |
| `freshness` | `{}`; compares revision, spec/design hashes, Git state. |
| `preview` | `patch`, `expected_revision`; equivalent to update dry-run. |
| `impact` | `tasks` (initial IDs), optional rooted `paths`; transitive dependents, evidence, review, path overlaps. |
| `fingerprint` | `references`: `{root,path,sha256?}` array; local file hashes and comparisons. |
| `readiness` | Optional `required_checks`; closure findings grouped by task, output, receipt, review, reflection, memory and index, plus `worktrees` (`path`, `exists`, `missing`) and `checks` (`missing`, `unreportable`). |
| `memory-health` | `{}`; `absent`, `no_matches`, `available` or `failed`, with diagnostics/candidates. |
| `recover` | `id`, optional boolean `apply`; inspect a journal, or resume metadata/index persistence only. |

Documents are `spec`, `planning`, `architecture` (the design), `handoff`, `reflection`,
`memory-staging`, `evidence`, `tasks/T01`, `reviews/review_01`, or `artifacts/report`. Exact `entry`
selects a decision, finding or evidence record ID; `section` selects an exact heading, including
planning sections. Reads default to 2,000 characters, maximum 20,000, with `next_offset` and
truncation metadata. `next_offset: null` ends pagination; `truncated` also marks pages omitting
earlier content. `if_sha256` suppresses unchanged content only if the caller still holds it; after
context loss, reread. Handoff reads omit the generated metadata block.

```json
{"task":"T01","max_chars":12000,"selections":[
  {"document":"handoff"},
  {"document":"spec","section":"Constraints and important assumptions"},
  {"document":"planning","section":"Approach and key decisions"},
  {"document":"evidence","entry":"ev-<saved-id>"}
]}
```

The bundle caps source bodies at 12,000 characters by default (maximum 40,000); it lists omitted
selections; follow their pagination. Fingerprints read at most 50 selected files totaling 16 MiB.
Matching hashes do not establish undeclared/remote freshness, authority, acceptance or stopped
processes. Impact reports candidates, never automatic invalidation.
