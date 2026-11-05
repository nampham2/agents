# Fresh coordinator sessions

Use this for handoff or interruption recovery. One coordinator writes at a time; a
note neither launches a replacement nor transfers ownership. Read
[handoff-writing.md](handoff-writing.md) before preparing a note or brief.

## When to hand off

Requirements confirmation, planning agreement, task planning, execution milestones and delivery
review are checkpoints, not stops ([stopping.md](stopping.md)). Restart on user request or
`context-pressure`.

Maintain records under [durable-context.md](durable-context.md) and reserve context to checkpoint.
Restart only when recovery justifies it.

## Prepare and yield

1. Finish a small in-flight operation or preserve partial outputs and the next step.
2. Settle launched workers/commands: observe completion, or stop and confirm termination,
   before transferring write scope. Check native-worker notes and `execution_active`. A
   missing handle or stale heartbeat does not prove termination. Unknown ownership blocks
   conflicting writes. Active legacy ownership requires [legacy-executor.md](legacy-executor.md).
3. Save owning records, confirmations and evidence. During alignment, save drafts and open
   questions; invent no agreement.
4. Run `context <project-dir> --validate`. Inspect target changes, branch/commit, dirty-path
   ownership, receipts. Resolve errors or name the blocker. A `handoff_banner` means
   handoff.md predates the state: trust `project.json` and checkpoint again. `resume` returns
   `do_not` and `closure_hint`.
5. Use [`workflow checkpoint`](automation.md) with continuation fields and the handoff token; it
   returns a resume prompt. Mark `Session state: ready for handoff` only after validation and
   ownership are resolved, else `blocked`. Retain the token; later record changes need refresh.
   If saving is blocked, report unsaved essentials and handles.
6. Give the project path, restart reason, blockers and this prompt; then end the turn:

   ```text
   Use the project skill to resume <absolute-project-dir> in this fresh session.
   Read its handoff and validate current state before continuing.
   Checkpoint: revision <N>, handoff SHA-256 <token>.
   Next: <one action or recovery blocker>.
   ```

The user opens the replacement session; if they continue here, recheck the checkpoint. After
transfer, the old coordinator stays inactive until control is transferred back.

## The continuation note

Keep one live note, normally under 600 words, never omitting unresolved ownership or essential
constraints:

- Date, reason, session state, phase, revision, spec/design tokens, agreement/review ID.
- Next action and ordered file/section pointers.
- Open decisions, pending confirmation, superseded direction, unfinished reconciliation.
- Partial outputs, branch/commit and dirty-path ownership; evidence, failures, remaining checks.
- Worker/command handles and states, authorization/receipt pointers, uncertain effects (or none).
- Prior-lesson assessments and pending staging/checks, or none/unavailable.

Link history and owning records; do not copy. Derived tokens/revisions are hints, not proof
of freshness or ownership.

## Resume

Start with [`workflow resume`](automation-context.md), following pagination. Inspect freshness
findings, target changes and ownership even when tokens match. Re-check consequential external
claims with the note's commands; `UNVERIFIED` claims block work that relies on them. Check a
brief's expiry and token first. A verifier fault leaves the verdict unknown. Reuse valid agreement
and lesson assessments; load only the next step's inputs.

For a stale or missing note, reconcile affected claims from canonical records, decisions, evidence
and partial files. Retain an unreadable note, use ordinary context and separate validation, and
repair it. Ask only about unrecoverable facts.

Complete pending [change reconciliation](execution-changes.md) before affected execution; a new
planning agreement does not update assignments or acceptance evidence. Missing or draft agreement
returns to its alignment step; resume interrupted interviews.

Resolve native/legacy ownership before conflicting writes; a new conversation does not terminate
old commands. Inspect uncertain external outcomes before retrying; no receipt is not proof of
no effect. Preserve scoped authorization and inspect partial work before resuming a task. Report
discrepancies and the next action, mark the note `working`, and continue.
