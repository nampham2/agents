# Optional executor and recovery

Use `run-auto <project-dir> [--concurrency N]` only when authorized and useful. Default capacity is
two. Admission requires v4, a clean Git target, `none`/`local_write` effects, target-file outputs,
exhaustive `reads`, separate read-only `test`/`[` checks for required outputs, and an available
host CLI. Ordinary test suites and unenumerable inputs do not fit. Continue sequentially in those
cases; never weaken verification or run the executor merely to obtain a refusal.

Workers use isolated worktrees. Inspect `execution/runtime/automatic-run.json`: `completed` is
already evidenced/committed; `deferred` can enter another wave; `fallbacks` describes sequential
handoffs; `blocked` requires recovery. Exit 2 means no task completed. Do not repeat structural
refusals. v3 activation requires `enable-execution` and the legacy-writer quiescence attestation;
see [workspace-schema.md](workspace-schema.md) for that operation.

When context reports `execution_active`, inspect the journal before mutating or starting task
work. A stale process or missing heartbeat does not prove it stopped. Preserve journals, grants,
worktrees, and partial outputs. Never clear ownership fields or replay uncertain effects to force
progress. Native worker ownership is separate; see [task-workers.md](task-workers.md).

For protocol recovery, read the relevant sections of [parallel-execution.md](parallel-execution.md):
§12 ownership/takeover, §13 recovery and partial outputs, §8.2 crash-prefix completion. Locate them
by heading and read their explicit dependencies. The full protocol is for executor development,
not routine task execution. Recovery cannot be inferred from this short guide; if live ownership
cannot be resolved through available observation/stop capabilities, preserve the blocker.
