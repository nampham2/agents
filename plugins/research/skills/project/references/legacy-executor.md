# Retired automatic executor

The automatic executor was removed in 0.17.0. Use normal task execution and scoped native workers.
`run-auto`, `run-once`, `run-parallel` and `enable-execution` are no longer commands. New projects
create no `execution/` directory; the idle v4 JSON fields remain for format compatibility.

Old `execution/config.json` contains concurrency limits, timeouts and filesystem-probe results,
not project knowledge. `runtime/automatic-run.json` summarizes a past automatic pass; fallback-only
reports do not mean workers ran. Used stores can also hold plans, attempts, grants, process handles,
worktrees and recovery journals. Preserve them; never use this directory for notes or handoffs.

Existing idle v3/v4 projects can continue without migration or cleanup. This release does not delete
or rewrite legacy stores, and their presence alone does not require loading them on every resume.

If context reports `execution_active`, stop conflicting work. This version retains the write guard
but cannot stop or recover the old executor. Report the project path and ownership blocker; ask the
user to arrange recovery with the compatible previous installation before resuming here. Do not
clear ownership fields, delete locks/journals, reinterpret a stale heartbeat as termination, or
replay uncertain effects. Do not run old and new coordinators concurrently. Native-worker ownership
is separate and must still be resolved through task notes and the host's observation/stop tools.
