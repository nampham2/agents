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

This is a `round` input. It saves the supplied sections, decisions, optional architecture and
continuation together. Existing architecture/handoff require their tokens too. It does not infer
answers, advance phase, or mark agreement. `sections` uses the ordinary spec heading/body mapping.

| Action | Additional input and behavior |
| --- | --- |
| `checkpoint` | `continuation`; derives phase, revision, spec/design tokens and Git observations; returns saved tokens and a resume prompt. |
| `round` | Optional `sections`, `decisions` (`{id,body}`), `architecture`, `continuation`. |
| `confirm` | `kind` (`requirements` or `architecture`), exact `proposal_sha256`, `review_id`, actual `response`, `source`, `scope`; optional `continuation`. Appends confirmation; architecture also updates its status and spec link. |
| `correct` | Terminal `task`, `reason`, `replacement` with new task fields except ID/status/evidence/authorization/receipts; optional `continuation`. Allocates a new ID and links prior work without copying consent or acceptance. |
| `reconcile` | Explicit `patch`, `decision`, `continuation`; saves selected dispositions, rewiring and pointers through normal commit guards. |
| `authorize` | `task`, full `authorization`: `required`, `status`, `scope`, `source`, `authorized_at`. |
| `receipt` | `task`, actual `receipt`: `kind`, `value`, `destination`, `timestamp`; exact duplicates are not appended. |
| `review` | `reviewer`, `version`, `scope`, `findings`, actual `status`; optional `evidence` references and `required`. Allocates the next delivery cycle/file, preserving history. Use `pending` to reopen acceptance. |
| `finalize` | `reflection`, `continuation`; validates and commits DONE, then saves the final handoff from that committed revision. |
| `maintenance` | `reason`, optional `tasks`, `continuation`; reopens DONE as PLANNING. |
| `cancel` | `reason`, explicit `tasks` dispositions, `continuation`; commits CANCELLED without deleting work or claiming workers stopped. |
| `assess` | `topic`, `disposition` (`apply`, `reject`, `defer`), `reason`, `application`; saves the lesson assessment with its actual topic token. |
| `report` | Optional canonical report `sections` and boolean `graph`; generates requested Markdown scaffold/accounting and checks generated citations. |

Continuation fields are strings: `next` (required on first checkpoint), `questions`, `pointers`,
`partial`, `ownership`, `effects`, `lessons`, `session`. Only supplied fields change; empty text
explicitly resolves a prior field. Session labels: `working`, `waiting`, `blocked`, `ready for
handoff`. Claim readiness only after validation and actual ownership/effect reconciliation.
Before supplying continuation, read [handoff-writing.md](handoff-writing.md). Put verifier status
in `next`/`partial`, sourced external observations in `ownership`/`effects`, and the literal
`## Do not` list inside `effects`; these are Markdown strings, not new input fields. Checkpoint
metadata does not verify their prose, and merged fields retain their original observation times.
Existing free-form handoffs require `import_legacy: true`; their full text is preserved.
An unchanged checkpoint is not rewritten. Scripts derive metadata, not the next decision.

Requirements confirmation does not approve architecture. Architecture confirmation requires its
review identifier in the saved proposal and never authorizes effects or accepts delivery review.
Scripts validate record shape and tokens; agents remain responsible for truthful quoted consent.

Reports are opt-in. Supply conclusions and limitations; missing sections remain marked unwritten.
Generated citation findings concern file/anchor integrity, not claim support or arbitrary prose
links. Use the existing report reference/checker for requested HTML and presentation requirements.
