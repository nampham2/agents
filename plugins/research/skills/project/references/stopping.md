# When work may stop

Use this when deciding whether to stop after a task. The default is to continue. `task finish`,
`task skip`, `task block`, `resume` and, at policy 6, `task start` return `continue` (a task ID,
`plan` or `finalize`) or one `stop_reason`, derived from the records. Both are advice and grant no
authorization.

A checkpoint whose `session` is `waiting`, `blocked` or `ready for handoff` must carry one of
these in `stop_reason`. A `working` session carries none.

- `user-decision`: a choice only the user owns is open, such as a requirement, a design, a scope
  change, an agreement to confirm, an exhausted automated decision, or a host with no fresh agents
  at policy 6.
- `authorization`: the next task has a destructive or external effect with no recorded
  authorization.
- `blocked`: no task is ready after a real attempt to unblock the work. Name the blocker.
- `context-pressure`: host telemetry or observed loss of relevant context makes reliable
  continuation difficult. A phase change, a tool count or a compaction alone is not enough. Do not
  invent token thresholds.
- `closure-step`: the merge, a worktree keep or remove choice, a delivery review or the project
  commit needs the user's words.
- `closed`: the project is `DONE` or `CANCELLED`.

A pending automated-mode offer is in `move_detail` and is asked once ([modes.md](modes.md)); it is
not a stop.

These are not reasons: a task finished, a phase changed, a summary is ready, or a wish for
reassurance. Do not ask whether to continue. Report the result and start the next task in the same
turn.

When resume reports `crash_loop` or a due wait, or a lock is busy, read
[durability.md](durability.md) before deciding to stop.
