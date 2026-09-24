# automation context

## Reads and preparation

Prefer `init ... --json` for path, revision, roots, document tokens and warnings without another
lookup. `update ... --dry-run --expected-revision N` previews the ordinary small patch without
writing; it returns changed fields and canonical validation findings.

| Action | Input fields and result |
| --- | --- |
| `resume` | Optional `task`, `selections`, `max_chars`; validation, context, source slices, freshness, memory health and unresolved worker observations. |
| `packet` | Same as resume, plus optional `worker`, `scope`, `lessons` supplied by coordinator. Packaging neither dispatches nor authorizes. |
| `read` | Required `document`; optional `section` or `entry`, `offset`, `max_chars`, `if_sha256`. |
| `freshness` | `{}`; compares checkpoint revision, spec/design hashes and observed Git state. |
| `preview` | `patch`, `expected_revision`; equivalent to update dry-run. |
| `impact` | `tasks` (initial IDs), optional rooted `paths`; transitive dependents, evidence, review and declared path overlaps. |
| `fingerprint` | `references`: array of `{root,path,sha256?}`; selected local file hashes and comparison results. |
| `readiness` | `{}`; existing closure findings grouped by task, output, receipt, review, reflection, memory and index. |
| `memory-health` | `{}`; `absent`, `no_matches`, `available` or `failed`, with bounded diagnostics/candidates. |
| `recover` | `id`, optional boolean `apply`; inspect a journal, or explicitly resume metadata/index persistence only. |

Documents are `spec`, `architecture`, `handoff`, `reflection`, `memory-staging`, `evidence`,
`tasks/T01`, `reviews/review_01`, or `artifacts/report`. Exact `entry` selects a saved decision,
finding or evidence record ID; `section` selects an exact heading, including architecture sections.
Reads default to 2,000 characters, maximum 20,000, with `next_offset` and truncation metadata.
`next_offset: null` ends pagination; `truncated` also marks pages that omit earlier content.
`if_sha256` suppresses unchanged content only when that content is still retained by the caller.
After context loss, request the needed bodies again.
Handoff reads omit the generated metadata block; freshness compares those machine fields.

```json
{"task":"T01","max_chars":12000,"selections":[
  {"document":"handoff"},
  {"document":"spec","section":"Constraints and important assumptions"},
  {"document":"architecture","section":"Interfaces"},
  {"document":"evidence","entry":"ev-<saved-id>"}
]}
```

The bundle caps selected source bodies at 12,000 characters by default (maximum 40,000), not the
lossless selected assignment. It lists omitted selections; follow their pagination as needed.
Fingerprints read at most 50 explicitly selected files totaling 16 MiB; use configuration files
for environment inputs. Matching hashes do not establish undeclared/remote freshness, authority,
semantic acceptance or stopped processes. Impact reports candidates, never automatic invalidation.
