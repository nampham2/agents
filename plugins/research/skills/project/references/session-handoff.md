# Fresh coordinator sessions

Use this reference when preparing/receiving a handoff or recovering an interruption. Only one
coordinator writes at a time; a note neither launches a replacement nor transfers ownership.
Read [handoff-writing.md](handoff-writing.md) before preparing the note or a companion brief.

## When to hand off

Requirements confirmation, architecture agreement, planning, execution milestones and delivery
review are checkpoint opportunities. Continue authorized work in the same session by default.
Restart on user request, or when accumulated context makes reliable continuation difficult.
Use available host telemetry and observed loss of relevant context; do not invent token thresholds.
A phase change, tool count or compaction alone is not a restart trigger.

Maintain records under [durable-context.md](durable-context.md). Reserve context to checkpoint.
Fresh sessions can reduce peak context but also repeat instruction and project reads; restart only
when its recovery benefit justifies that cost.

## Prepare and yield

1. Finish a small in-flight operation or preserve partial outputs and the exact next step.
   A session break changes neither task completion nor project phase. Preserve the working tree.
2. Settle launched workers and commands: observe completion or stop and confirm termination before
   transferring their write scope. Check native-worker notes as well as `execution_active`.
   An unavailable handle or stale heartbeat does not prove termination. Unknown ownership blocks
   conflicting writes. Active legacy ownership requires [legacy-executor.md](legacy-executor.md);
   do not bypass document guards.
3. Save owning records, actual confirmations and evidence. During alignment, save drafts and open
   questions without inventing tasks or agreement.
4. Run `context <project-dir> --validate`. Inspect target changes, applicable branch/commit and
   dirty-path ownership; inspect receipts for uncertain effects. Resolve validation errors or name
   the recovery blocker.
5. Use [`workflow checkpoint`](automation-records.md) with continuation fields and the handoff
   token; it derives revision/source/Git metadata and returns a resume prompt.
   Mark `Session state: ready for handoff` only after validation and ownership are resolved;
   otherwise mark `blocked`. Check the write result and retain its token. Later record changes
   require a refresh. If saving is blocked, report unsaved essentials and handles in the response.
6. Supply the project path, restart reason, blockers and this concrete prompt; then end the turn:

   ```text
   Use the project skill to resume <absolute-project-dir> in this fresh session.
   Read its handoff and validate current state before continuing.
   Checkpoint: revision <N>, handoff SHA-256 <token>.
   Next: <one action or recovery blocker>.
   ```

The user opens the replacement session. If they choose to continue here, recheck the checkpoint and
proceed without insisting on a restart. After transfer, the old coordinator stays inactive until
the user explicitly transfers control back.

## The continuation note

Keep one live note, normally under 600 words; never omit unresolved ownership or essential
constraints for size. Include only what the successor needs:

- Date, reason, session state, phase, revision, spec/design tokens and agreement/review ID.
- Next action and ordered file/section pointers explaining what to read.
- Open decisions, pending confirmation and any superseded direction or unfinished reconciliation.
- Partial outputs, branch/commit and dirty-path ownership; evidence, failures and remaining checks.
- Worker/command handles and observed states, authorization/receipt pointers and uncertain effects;
  explicitly say when none exist.
- Relevant prior-lesson assessments and pending staging/checks, or none/unavailable.

Link history and owning records rather than copying them. Derived tokens/revisions are hints;
they cannot certify semantic freshness, truth, permission or ownership.

## Resume

Start with [`workflow resume`](automation-context.md), which includes freshness, using needed
source/task selections and following pagination. Inspect freshness findings, target changes and
ownership independently of token matches. Re-check consequential external claims with the note's
commands; unresolved `UNVERIFIED` claims block work that relies on them. Check a brief's expiry and
handoff token before following its instructions. A verifier fault leaves the subject verdict
unknown.
Reuse valid agreement; load only the next step's requirements, design, dependencies and findings.
Reuse local lesson assessments; reopen shared sources only for missing detail or changed conditions.

For stale/missing notes, reconcile affected claims from canonical records, decisions, evidence and
partial files. An unreadable note can break validated context: retain it, use ordinary context and
separate validation, and repair it before depending on it. Ask only for unrecoverable facts.

Complete pending [change reconciliation](execution-changes.md) before affected execution; new
architecture agreement alone does not update assignments or acceptance evidence. Missing/draft
agreement returns to the affected alignment step; interrupted interviews continue open questions.

Resolve native/legacy ownership before conflicting writes. A new conversation does not terminate
old commands. Inspect uncertain external outcomes before retrying; absence of a receipt is not proof
nothing happened. Preserve scoped authorization and inspect partial work before resuming an existing
task. Briefly report discrepancies and the next action, mark the note `working`, and continue.
