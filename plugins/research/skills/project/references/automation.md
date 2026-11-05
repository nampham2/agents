# Lifecycle automation

Use the launchers beside the loaded skill (Codex) or on PATH (Claude). Examples use
`research-project`; substitute the resolved executable, never a Python module.

```sh
research-project workflow <project-dir> <action> -
```

Send one JSON object on stdin (`-`) or a file path. Unknown fields fail. Named commands do
bookkeeping; decisions, confirmations, chosen checks and acceptance stay agent-authored. They expand
no permission.

Records are below. Other references:

- [Context](automation-context.md): `resume`, `packet`, conditional/exact `read`, `freshness`,
  `preview`, `impact`, `fingerprint`, `readiness`, `memory-health`, and `recover`.
- [Execution and memory](automation-execution.md): native `worker-event` observations,
  durable `verify` check batches, retry-safe `triage`.

`workflow <project-dir> <action> --schema` prints allowed, required, nested and variant keys. Writes
return tokens, revisions and partial outcomes. A new ID is for new work, not for bypassing a
conflict; inspect unknown outcomes, never replay. A draft or passing check does not establish
agreement, effect authorization, task acceptance or stopped workers.

## Guarded records

Every metadata write requires `id` (lowercase letters/digits/hyphens, at most 64 characters) and
`expected_revision`, plus `tokens` mapping each existing document it changes to its SHA-256; new
documents default to `missing`. Retain the exact input for retries: the same ID/input completes the
operation; changed input with the same ID conflicts.

```json
{"id":"round-2","expected_revision":0,"tokens":{"spec":"<sha256>"},
 "sections":{"In scope":"Parse the existing format; preserve compatibility."},
 "decisions":[{"id":"format-decision","body":"User chose compatibility; source: current reply."}],
 "planning":"# A2\n\nStatus: draft\n\n<complete current proposal>",
 "continuation":{"next":"Ask for A2 confirmation","questions":"Confirm A2?"}}
```

| Action | Additional input and behavior |
| --- | --- |
| `checkpoint` | `continuation`; derives phase, revision, tokens and Git observations; returns tokens and a resume prompt. |
| `round` | Saves optional `sections` (spec heading to body), `decisions` (`{id,body}`), `planning`, `continuation`; existing planning/handoff need tokens; `architecture` writes the design. |
| `confirm` | `kind` (`requirements`, `planning` or `alignment`; old name `architecture`), exact `proposal_sha256` (`alignment`: `requirements_sha256` and `planning_sha256`), `review_id`, actual `response`, `source`, `scope`; optional `continuation`. Appends a marked confirmation; planning also updates its status and spec link. |
| `correct` | Terminal `task`, `reason`, `replacement` (new task fields except ID/status/evidence/authorization/receipts); optional `continuation`. Allocates a new ID without copying consent or acceptance. |
| `reconcile` | Explicit `patch`, `decision`, `continuation`; saves dispositions, rewiring and pointers through commit guards. |
| `authorize` | `task`, full `authorization`: `required`, `status`, `scope`, `source`, `authorized_at`. |
| `receipt` | `task`, `receipt` (`kind`, `value`, `destination`, `timestamp`); `value` is an HTTP(S) URL or an id prefixed `receipt:`, `deployment:`, `message:`, `purchase:`, `publish:` or `commit:`; exact duplicates are skipped. |
| `review` | `reviewer`, `version`, `scope`, `findings`, actual `status`; optional `evidence`, `required`. Allocates the next cycle/file, preserving history. `pending` reopens acceptance. |
| `goldfish` | `operation` `open` (`target`, `stages`; diff adds `worktree`, optional `commit`) returns `round`, `token`, `reviewed`; `close` (`round`; `tokens` `{"reviews/goldfish_NN": token}`; `reports` `[{stage, summary}]`; `findings` `[{id, reviewer, severity, scope, summary, triage, evidence, resolution, artifact}]`; optional `confirmation` `{source, response}`) computes the outcome; `waive` (`target`, `reason`, `confirmation`; diff adds `worktree`, optional `commit`). A changed target needs a `FIX` finding; a Blocker or Major `FIX` on an unchanged target names an existing `artifact` file; one `reports` entry per opened stage; `commit` is full hex. Writes only `reviews/goldfish_NN.md`; see [goldfish-review.md](goldfish-review.md). |
| `finalize` | `reflection` (200+ characters, a heading, `## Retro`: what got in the way with pointers, unused references, one plugin change or `none`; required after the cutoff), `continuation`; `tokens` include `memory-staging`; commits DONE, saves the handoff; returns `reflected_lessons` (`staged`, `skipped`), retro and control warnings. |
| `maintenance` | `reason`, optional `tasks`, `continuation`; reopens DONE as PLANNING. |
| `cancel` | `reason`, `tasks` patch array, `continuation`; commits CANCELLED, deleting nothing and claiming no stopped workers. |
| `assess` | `topic`, `disposition` (`apply`, `reject`, `defer`), `reason`, `application`; saves the lesson assessment with its topic token. |
| `worktree` | `operation` `record` (`path`, `branch`, optional `repository`, `kind`, `role`) or `close` (`path`, `decision` `keep`/`accept_dirty`/`remove`), or a delivery record (`path` each): `pull_request`, `alpha`, `release`, `merge` (also `merged_head`); plus `confirmation`; Git-verified; see [worktrees.md](worktrees.md). |
| `report` | Optional report `sections` and boolean `graph`; generates the Markdown scaffold/accounting and checks citations. |

Continuation fields are strings: `next` (required on first checkpoint), `questions`, `pointers`,
`partial`, `ownership`, `effects`, `lessons`, `do_not`, `session`. Only supplied fields change;
empty text resolves a prior field. Session labels: `working`, `waiting`, `blocked`, `ready for
handoff`. Claim readiness only after validation. Before supplying continuation, read
[handoff-writing.md](handoff-writing.md). Put verifier status in `next`/`partial`, sourced
observations in `ownership`/`effects`, prohibitions in `do_not`; headings in values are refused.
Existing free-form handoffs require `import_legacy: true`; their text is preserved.

Requirements confirmation does not approve planning; only `alignment` records both from one reply.
Planning confirmation needs its review identifier in the saved proposal; it never authorizes effects
or accepts delivery review. A later substantive edit makes a confirmation stale (`edit` and `round`
return `stale_confirmations`); the gate refuses until reconfirmed.

Reports are opt-in; missing sections stay marked unwritten. Citations are checked for file/anchor
integrity only.
