# automation records

## Guarded records

Every metadata write requires `id` (lowercase letters/digits/hyphens, at most 64 characters) and
`expected_revision`. Include `tokens`, mapping every existing document the operation will change
to its current whole-document SHA-256; new documents default to `missing`. Reuse returned tokens.
Retain the exact input for retries: the same ID/input returns or completes that operation; changed
input with the same ID conflicts. Metadata operations never replay commands or external effects.

```json
{"id":"round-2","expected_revision":0,"tokens":{"spec":"<sha256>"},
 "sections":{"In scope":"Parse the existing format; preserve compatibility."},
 "decisions":[{"id":"format-decision","body":"User chose compatibility; source: current reply."}],
 "architecture":"# A2\n\nStatus: draft\n\n<complete current proposal>",
 "continuation":{"next":"Ask for A2 confirmation","questions":"Confirm A2?"}}
```

| Action | Additional input and behavior |
| --- | --- |
| `checkpoint` | `continuation`; derives phase, revision, spec/design tokens and Git observations; returns saved tokens and a resume prompt. |
| `round` | Saves optional `sections` (spec heading to body), `decisions` (`{id,body}`), `architecture` and `continuation` together; existing architecture/handoff need their tokens. Infers no answers, advances no phase, marks no agreement. |
| `confirm` | `kind` (`requirements`, `architecture` or `alignment`), exact `proposal_sha256` (`alignment`: `requirements_sha256` and `architecture_sha256`), `review_id`, actual `response`, `source`, `scope`; optional `continuation`. Appends a marked confirmation; architecture also updates its status and spec link. |
| `correct` | Terminal `task`, `reason`, `replacement` with new task fields except ID/status/evidence/authorization/receipts; optional `continuation`. Allocates a new ID and links prior work without copying consent or acceptance. |
| `reconcile` | Explicit `patch`, `decision`, `continuation`; saves selected dispositions, rewiring and pointers through normal commit guards. |
| `authorize` | `task`, full `authorization`: `required`, `status`, `scope`, `source`, `authorized_at`. |
| `receipt` | `task`, actual `receipt`: `kind`, `value`, `destination`, `timestamp`; `value` is an HTTP(S) URL or an id prefixed `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:` or `commit:`; exact duplicates are not appended. |
| `review` | `reviewer`, `version`, `scope`, `findings`, actual `status`; optional `evidence` references and `required`. Allocates the next delivery cycle/file, preserving history. Use `pending` to reopen acceptance. |
| `finalize` | `reflection` (at least 200 characters with a heading), `continuation`; validates and commits DONE, then saves the final handoff; returns staged-lesson warnings. |
| `maintenance` | `reason`, optional `tasks`, `continuation`; reopens DONE as PLANNING. |
| `cancel` | `reason`, explicit `tasks` dispositions, `continuation`; commits CANCELLED without deleting work or claiming workers stopped. |
| `assess` | `topic`, `disposition` (`apply`, `reject`, `defer`), `reason`, `application`; saves the lesson assessment with its actual topic token. |
| `worktree` | `operation` `record` (`path`, `branch`, optional `repository`, `kind`, `role`) or `close` (`path`, `decision` `keep`/`accept_dirty`/`remove`), plus quoted `confirmation`; verifies against Git and repoints the target; see [worktrees.md](worktrees.md). |
| `report` | Optional canonical report `sections` and boolean `graph`; generates requested Markdown scaffold/accounting and checks generated citations. |

Continuation fields are strings: `next` (required on first checkpoint), `questions`, `pointers`,
`partial`, `ownership`, `effects`, `lessons`, `do_not`, `session`. Only supplied fields change;
empty text explicitly resolves a prior field. Session labels: `working`, `waiting`, `blocked`,
`ready for handoff`. Claim readiness only after validation and actual ownership/effect
reconciliation. Before supplying continuation, read [handoff-writing.md](handoff-writing.md). Put
verifier status in `next`/`partial`, sourced external observations in `ownership`/`effects`, and the
prohibitions in `do_not`; values are body text, and headings in them are refused. Metadata does
not verify the prose; merged fields keep their observation times.
Existing free-form handoffs require `import_legacy: true`; their full text is preserved. An
unchanged checkpoint is not rewritten.

Requirements confirmation does not approve architecture; only `alignment` records both from one
reply. Architecture confirmation needs its review identifier in the saved proposal and never
authorizes effects or accepts delivery review. A later substantive edit makes a confirmation stale
(`edit` and `round` return `stale_confirmations`); the gate refuses until it is confirmed again.
Scripts validate record shape and tokens; agents remain responsible for truthful quoted consent.

Reports are opt-in: supply conclusions and limitations; missing sections stay marked unwritten.
Citation findings cover file/anchor integrity, not claim support. Use the report reference for HTML
or presentation requests.
