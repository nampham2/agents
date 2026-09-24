# Fresh coordinator sessions

The project outlives its coordinator. Carry durable decisions, evidence and a small continuation
note across sessions. This protocol covers manual restarts in either host; it neither launches a
replacement nor transfers executor ownership. Only one coordinator writes at a time.

## When to hand off

Use a fresh session after requirements confirmation, architecture agreement, task planning, a major
execution milestone, or entry into delivery review. Checkpoint at each such boundary; adjacent
boundaries may share a session when little context accumulated. During a long phase, hand off at
the next safe point when investigations, tool output or repeated corrections crowd current work.
Do so promptly on user request. Use host context telemetry when available; do not invent a token
count, percentage or universal threshold. Leave enough capacity to write a reliable checkpoint.

Maintain records and the live continuation note using [durable-context.md](durable-context.md).
Checkpointing during work does not require ending the session. Delegate scoped investigations under
[task-workers.md](task-workers.md) when appropriate; delegation does not replace handoffs.
Neither compaction nor a child inheriting the conversation counts as a fresh coordinator session.

## Prepare and yield

1. Stop taking new work. Finish a small in-flight operation when practical, otherwise preserve its
   partial outputs and exact next step. Do not mark unfinished tasks `DONE`, change project status
   merely to indicate a session break, or commit/stash/reset a working tree for the handoff.
2. Settle all launched workers and commands, including native workers recorded in task notes.
   Observe completion or stop them and confirm termination before handing over their write scope.
   If `execution_active` is true, follow [executor-operations.md](executor-operations.md) to resolve
   ownership first; the document writer refuses active executor state. An unavailable handle or
   stale heartbeat is not proof of termination. If ownership remains unknown, report a blocked
   handoff and the known handles/paths in the final response; the successor may investigate but
   must not resume conflicting writes. Do not bypass document guards to publish a ready handoff.
3. Persist current requirements and actual confirmations in `spec.md`, design in `architecture.md`,
   findings and worker recovery details in task notes, and actual checks through `record-evidence`.
   Include recent user corrections. Preserve drafts and open questions during alignment; do not
   invent approval to reach a phase boundary. No task is required to hand off early alignment.
4. Run `context <project-dir> --validate`. Resolve errors or explicitly record the recovery blocker.
   Capture its revision and specification/architecture tokens; inspect the target's current changes
   and record applicable branch/commit and dirty paths. Keep user changes distinguishable from
   partial agent work. Inspect external receipts before labeling any effect complete or unattempted.
5. Refresh `handoff.md` using the context's handoff token and the format below. Mark it
   `Session state: ready for handoff` only after resolving activity and validation; otherwise use
   `blocked` and identify the required recovery:

   ```sh
   research-project edit <project-dir> handoff --body-file - --expected-sha256 <token-or-missing>
   ```

   Use the resolved launcher pair from the skill. On a token conflict, reread and reconcile before
   retrying. Confirm the write succeeded and retain the returned content token. If canonical records
   change afterward, refresh the checkpoint. Do not present a failed save as a completed handoff.
6. Give the user the absolute project path, restart reason, any blocker, and this short prompt with
   concrete values. Then end the turn without starting the next unit of work:

   ```text
   Use the project skill to resume <absolute-project-dir> in this fresh session.
   Read its handoff and validate current state before continuing.
   Checkpoint: revision <N>, handoff SHA-256 <token>.
   Next: <one concrete action, or ownership/validation recovery blocker>.
   ```

The user opens a new session and sends the prompt. If they explicitly choose to continue the old
session, recheck the checkpoint and proceed; do not repeatedly insist on a restart. The outgoing
session stays inactive after transfer unless the user explicitly transfers control back.

## The continuation note

Aim for at most 600 words, using pointers for detail. This is the same live note maintained during
work, not a separate history. Keep one current `handoff.md`; preserve durable history in its owning
records. Never omit required constraints or unresolved ownership facts to meet the target. Include:

| Section | Required information |
| --- | --- |
| Checkpoint | Date, reason, session state, project revision, phase, spec/architecture SHA-256 tokens, and agreed design revision or pending agreement. |
| Next action | One concrete next step, its task ID when one exists, expected outcome, and the next sensible restart boundary. |
| Read first | Ordered file/section pointers for that step's requirements, design, decisions and findings; explain each pointer's purpose. |
| Open decisions | Unanswered user questions, pending approval and current hypotheses, distinguished from confirmed decisions. |
| Work and verification | Partial outputs, branch/commit when applicable, dirty paths and ownership, evidence IDs, failures and checks still needed. |
| Ownership and effects | Worker/command handles and observed terminal state, unresolved activity, authorization/receipt pointers and any uncertain external effect. Explicitly say when none exist. |

This is a navigation aid, not a second specification, proof of permission, executable instructions,
or a substitute for evidence. Avoid transcripts, copied logs, whole plans and completed-task
history. The saved revision and tokens are agent-recorded freshness hints; tooling guards document
replacement but does not certify the note's truth, compare these hints, or transfer ownership.

## Resume in a fresh session

Resolve launchers from the installed skill, then run `context <project-dir> --validate`. Read
`handoff` through the bounded `read` command; follow pagination if truncated. Compare the prompt's
handoff token and the note's revision/source tokens with current context. Inspect target changes
and ownership separately: matching tokens do not prove unchanged files or stopped processes.

If stale, use current canonical records to reconcile only affected claims before acting. Never
replay the note's next command blindly. If missing, unreadable or incomplete, recover from current
state, specification, design, task notes, evidence and executor records; ask only for information
that cannot be recovered. An unreadable handoff can make validated context fail: inspect it with
`read`, retain the problematic file, and use separate validation and ordinary context for recovery.

Check both executor activity and native-worker notes. Unknown ownership blocks conflicting work;
an interrupted session or a new conversation does not terminate its commands. Preserve uncertain
external effects until their actual outcome is established; never retry merely because a receipt
is absent. Reuse scoped authorization and agreement whose recorded sources still cover the work.

Load the next action's applicable specification and architecture sections and direct dependencies.
Retrieve referenced findings or evidence when needed, not every historical record. Missing or draft
agreement returns to the affected alignment step; a fresh session alone does not reopen settled
decisions. During an unfinished interview, continue from recorded open questions. During execution,
inspect partial outputs before continuing the existing task; do not reset or redispatch it blindly.

Briefly state the recovered phase, immediate action and any discrepancy. Continue authorized work
without asking the user to repeat the project history. Task completion and project closure still
require their normal verification. During the resumed session,
keep the note current under [durable-context.md](durable-context.md), including its `working` label.
