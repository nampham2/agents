# When work may stop

Use this when deciding whether to stop after a task. The default is to continue. `task finish`,
`task skip`, `task block` and `resume` return `continue` (a task ID, `plan` or `finalize`) or one
`stop_reason`, derived from the records. Both are advice and grant no authorization.

A checkpoint whose `session` is `waiting`, `blocked` or `ready for handoff` must carry one of
these in `stop_reason`. A `working` session carries none.

- `user-decision`: a choice only the user owns is open, such as a requirement, a design, a scope
  change or an agreement to confirm.
- `authorization`: the next task has a destructive or external effect with no recorded
  authorization.
- `blocked`: no task is ready after a real attempt to unblock the work. Name the blocker.
- `context-pressure`: host telemetry or observed loss of relevant context makes reliable
  continuation difficult. A phase change, a tool count or a compaction alone is not enough. Do not
  invent token thresholds.
- `closure-step`: the merge, a worktree keep or remove choice, a delivery review or the project
  commit needs the user's words.
- `closed`: the project is `DONE` or `CANCELLED`.

These are not reasons: a task finished, a phase changed, a summary is ready, or a wish for
reassurance. Do not ask whether to continue. Report the result and start the next task in the same
turn.
