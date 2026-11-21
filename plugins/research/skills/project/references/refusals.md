# Refusals of coordinator and automated modes

Read this only when a coordinator-mode or automated-mode refusal needs its next step. Each row
gives the leading, fixed part of the text; the tool fills the braces. The codes are this file's
own lookup labels, not text the tool prints. The findings of the mode gate may begin with
`task <id>: ` or `task <id> cannot finish: `; the fixed text follows that prefix. The `cancel` and
`maintenance` rows share one text: the tool prints the action name for `<action>`. Modes are
described in [modes.md](modes.md).

| Code | Fixed text begins | Next step |
| --- | --- | --- |
| G0 | `coordinator mode cannot read the worker attempts:` | repair or remove the damaged line in `tasks/<id>.md` by hand |
| G1 | `coordinator mode: attempt ... is still open` | record `stopped` (or the real outcome) with `workflow worker-event` |
| G2 | `coordinator mode needs a completed worker attempt` | dispatch a fresh worker and record its events, or ask the user to turn `coordinator` off |
| R2 | `coordinator mode needs a replan statement` | review the unstarted tasks, then `task finish --replan none` or `changed` with `--replan-reason` |
| K1 | `coordinator mode: effect.kind cannot change from` | skip the task and add a new task for the exempt step |
| C0 | `assignment_counter is written by the tool:` | leave the counter as the tool stamps it |
| A1 | `authorization can change only through 'workflow authorize'` | create the task with its default authorization, then authorize it with the user's words |
| B0 | `task ... cannot finish: --backfill is refused in coordinator mode` | start the task, dispatch a worker, use a plain `task finish` |
| RA | `--replan applies only to task finish` | pass the flags to `task finish` only |
| R0 | `--replan needs coordinator mode` | drop the flags, or have the user turn `coordinator` on |
| R1 | `--replan takes none or changed, with --replan-reason` | give both flags, a reason of one line of at most 200 characters |
| R3 | `--start-next is refused with --replan changed` | finish first, then start the next task |
| valid decision prefix | `automated mode needs a valid decision: ` | every `needs a valid decision` row below starts with this text |
| no decision | `no current '<kind>' decision for` | `workflow decide` with the choice the text names |
| evidence round | `... is at evidence round` | collect evidence, record the same kind and subject again |
| exhausted | `... is exhausted after` | show the options to the user; record the user's answer |
| chose not | `... chose '...', not '...'` | record the needed choice with evidence, or ask the user |
| stale | `the '<kind>' decision for ... is stale` | the judged state changed: record the decision again |
| remove | `worktree close: recording remove is refused while options.automated is on` | record `keep` with the user's words, or stop for the user |
| keep, worktree gone | `the worktree is missing; restore it or record its removal:` | refused on recording `keep`: stop for the user, turn automated off with `workflow settings` and the user's words, then record remove with their confirmation |
| cancel | `workflow <action> needs the user's words while options.automated is on` | send the user's `confirmation` for cancel |
| maintenance | `workflow <action> needs the user's words while options.automated is on` | send the user's `confirmation` for maintenance |
