# Coordinator and automated modes

Read this at policy 6 before the first task. Two boolean options: `coordinator` (on by default)
and `automated` (off by default). Only `workflow settings` with the user's words changes them
([review-risk.md](review-risk.md)). `automated` needs `coordinator`; one call may turn both off.

## Coordinator mode

The coordinator breaks tasks down, orders workers, keeps canonical state, runs acceptance checks
and reviews, commits accepted work on the user's words and talks to the user. A commit is not a
task. A fresh worker executes every task except the exempt ones, the design task and routine
corrections included ([task-workers.md](task-workers.md)).

Each task: `task start`; `context --worker --task Tn` ([task-workers.md](task-workers.md));
`worker-event` `intent` ([automation-execution.md](automation-execution.md)); start the worker;
record `completed`; run the acceptance checks; the replan step; `task finish`.

`task finish` refuses a non-exempt task unless its notes hold, for the current assignment, a
worker attempt whose latest event is `completed` and no attempt still open (latest event neither
`completed` nor `stopped`). It also refuses a damaged worker line and `--backfill`. Every commit
path shares the check: `update`, `reconcile`, `commit` and the legacy `close` cannot mark such a
task `DONE`. It checks bookkeeping, not who edited a file. `task skip` needs no worker.

Exempt tasks (effect kind `external` or `destructive`) are run by the coordinator, not a worker.
Only `workflow authorize` grants consent. Split a task that mixes file work with an external step.
A started task cannot change into an exempt kind: skip it and add a new task.

A host with no fresh agents stops with `stop_reason` `user-decision`: the user turns `coordinator`
off. Turning it on mid-task credits no earlier work: dispatch a worker to complete the task.

A restart is a new assignment: after `task block` and `task start` a fresh worker must complete
the task. On a crash loop, record a finding or evidence for real progress. If the worker
completed, finish the task; if its attempt is open, record its outcome with `worker-event`. Do not
run `task block` then `task start`.

## The replan step

Before `task finish`, review the tasks that have not started against the outcome so far. Edit them
with `update`, `reconcile` and `workflow correct`: add, split, skip, reorder or rewire. Then
finish with `--replan none` or `--replan changed` and `--replan-reason "<one line>"`; exempt tasks
need it too. The tool does not verify the statement. `--start-next` is refused with `changed`. A
correction task for the finished task itself is added after the finish. Adding a task needs no
decision. A change of scope, outcome, interface or success criteria is not a replan: follow
[execution-changes.md](execution-changes.md) and ask the user.

## Automated mode: the offer

Offer the mode once, when the plan is agreed, a task is ready, `coordinator` is on and no
`automated` key is stored. `resume` and `task` results report the pending offer in `move_detail`.
Ask one prompt: whether to turn the mode on, and for standing words on two things only.

- Recorded worktrees are kept: the `worktree` `close` record, decision `keep`, quotes the reply.
- Which existing external tasks, by ID, are authorized: each gets an `authorize` record sourced from
  the reply; the `workflow worktree` delivery records of a named merge or release task quote it.

Record the answer with `workflow settings`: `options` `{"automated": true}` or `false` and a
`confirmation` quoting the reply. A decline stores `automated: false` and ends the offer;
`options: null` re-arms it. The user can turn it on later or off at any time. A task created after
the opt-in, a replacement too, stops for authorization if it is an external or destructive task.

## What still stops for the user

`settings`, `retarget`, the project commit, delivery-review acceptance, a Goldfish close, amend or
waive that needs a `confirmation`, `accept_dirty`, `remove` (the tool refuses it; never run `git
worktree remove`), an unauthorized external task, and a non-`FIX` triage of a Blocker or Major.
With automated on, workflow cancel and workflow maintenance need the user's words in a
confirmation. Never write your own words into a confirmation.

## Decisions

With `automated` on, record each choice that would be the user's with `workflow decide`. Four
kinds have a meaning; any other kind is free, self-rated, ungated and names its `options`.

| Kind | Subject | Choices | Assessor | Gate |
| --- | --- | --- | --- | --- |
| `task-change` | a task | `skip`, `keep` | required | a task newly `SKIPPED` needs `skip` |
| `replan` | the finished task | `changed`, `none` | self-rated | `--replan changed` needs `changed` |
| `task-accept` | a task | `accept`, `reject` | required | none: a prose rule |
| `project-close` | `project` | `close`, `continue` | required | project newly `DONE` needs `close` |

A pair is one kind with one subject.

The gates run on every commit path before any document is written. Replacing a task is a skip plus
a new task; the skip decision covers it, and skips made by a replan need their own.
`project-close` is refused while a task is not terminal. For `task-accept`, record `accept` before
finishing a task that deviates from the agreed design or has a failed check; do not finish it
while that pair is exhausted.

Keys: `by` (`agent` or `user`), `kind`, `subject` (a task ID or `project`), `choice`; `by` `agent`
adds `evidence`, `confidence`, `proposer` and, for a consequential kind, `assessor`. A listed kind
may leave out `options`. An evidence pointer is a record ID (`ev-` and 32 hex digits) or a file of
this project, with an optional `#<anchor>`. Every agent round cites at least one; an unresolved
pointer makes the record malformed.

Outcomes: `decision` at confidence 0.9 or above with a pointer; otherwise `evidence-round` on the
first and second such round and `exhausted` on the third. After `evidence-round`, collect more
evidence and record the same kind and subject again, citing at least one pointer: the tool checks
only that each pointer resolves, so use new evidence. Rounds count per pair; a qualifying record
decides in any round. An agent `decision` counts only while its judged state is unchanged: a
task's assignment, or the project's set of tasks and every reopen. Rounds,
exhaustion and user answers never go stale.

Exhaustion is sticky: `resume` and `task` results report `stop_reason` `user-decision` and quote the
options; show them to the user. The user answers at any time with `by` `user` and a `confirmation`
and the `spec` token, as for other `workflow` records (`context` returns the `tokens` map; `read ...
spec --outline` the spec token); the answer supersedes an earlier one and binds: the agent records
nothing for that pair while it stands. Both end when the task is terminal, a new answer comes,
`automated` goes off or the project is reopened.

For a consequential kind, start a fresh agent as assessor (the worker start, the decision brief as
its task); take back a confidence figure and a reason. Name its handle in `assessor`, which
differs from `proposer`. It records no worker event.

Each refusal names its next step; the fixed texts and what to do are in
[refusals.md](refusals.md).
