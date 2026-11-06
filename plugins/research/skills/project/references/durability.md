# Durability: effects, crash loops, locks, workers, waits

Read this when resume or context reports `effect_intents`, `crash_loop`, `waits`,
`silent_minutes` or `durability_warnings`, when a lock is busy, before an external or destructive
effect, or before waiving a Goldfish round. None of these records adds a `project.json` field, so
older launchers still read the project.

## Effect intents

Record an intent just before an external or destructive effect. If the session dies in between, the
next session then knows the effect may have run:

```json
{"id":"t05-push-intent","expected_revision":N,"task":"T05","operation":"intent",
 "key":"push feat/x to origin","effect":"git push origin feat/x"}
```

Send it to `workflow <project-dir> effect -`. Afterwards, clear it with `resolve`:

```json
{"id":"t05-push-resolve","expected_revision":N,"task":"T05","operation":"resolve",
 "key":"push feat/x to origin","outcome":"done","note":"origin/feat/x at abc1234"}
```

`resolve` is the normal path. A receipt clears an intent only when its `value` equals the key, so
this works only when you chose a key that is already a valid receipt value, such as
`commit:<sha>` or the URL you expect. A key like the example above is cleared only by `resolve`. An
intent whose key equals an existing receipt value on the task is refused.

When resume lists an unresolved intent (and `move_detail` says `effect may have happened`), check
the destination before retrying: the effect may have happened. Then resolve with what you found.
Intents work only on tasks with an external or destructive effect. A second intent with the same
open key, or a resolve without an intent, is refused.

Use the handoff `effects` field for the narrative ("pushed; PR not yet opened"). Use an intent for
the one command whose outcome a crash would leave unknown.

## Crash-loop stop

Resume counts project `workflow resume` calls since a RUNNING task's last progress. Progress means:

- a task command on it, including `--start-next`
- a receipt
- a finding
- an evidence row

At 3, resume stops with `stop_reason: blocked` and names each looping task with its count. Worker
packets, failed calls and status writers (`update`, `reconcile`, `cancel`, `maintenance`) do not
count as resumes or as progress.

A long task that compacts three times without recording anything stops too; that is expected. To
recover, record real progress (a finding or evidence), or check that the task is not looping and run
`task block` then `task start`. Either one resets the count.

## Locks

A busy lock reports its status:

- `live`: the owner runs on this host.
- `stale`: the owner's pid is dead, or the pid now belongs to a newer process.
- `unknown`: there is no `owner.json`, the owner is unreadable or legacy or from another host, the
  pid check failed, or its start time cannot be read.

The refusal always prints the status, its reason and the unlock command. Run the command only when
the status is `stale`; `unlock` refuses anything else:

```sh
research-project unlock <path> --workspace-root <root>
```

`unlock` accepts only a directory whose name ends in `.lock`, inside the root, holding nothing but
`owner.json`. It renames the lock to a tombstone and re-checks the owner. It removes the tombstone
only when the owner is unchanged and still stale; otherwise it restores the lock. It never removes a
live, unknown or foreign-host lock. A stale `.project.lock` blocks resume itself, so read the
refusal and unlock first.

Manual cases need a person to check:

- A lock left with a `.owner.json.*.tmp` file, from a crash during the write, stays unknown.
- Containers that share a volume and a hostname but run separate pid namespaces can report a false
  dead pid.
- On macOS the hostname can change with the network (`x.local` versus `x.corp`). A lock left before
  the change reads as `foreign host`; check that its pid is gone, then remove it by hand.
- If `unlock` reports a kept tombstone, the original path was taken while it worked. Inspect both
  paths.
- A holder that exits while its lock sits in a tombstone gets "cannot release". The restored lock
  then names a dead owner and needs another unlock.

## Silent workers

Each `unresolved_workers` entry shows `silent_minutes` once its latest event is more than 30
minutes old. Workers write no records, so the silence measures your observations: record a
`worker-event` with `event: observed` while a worker runs, and check the host before treating a
silent worker as gone.

## Waits

A BLOCKED task can carry a wait:

```json
{"id":"t07-wait","expected_revision":N,"task":"T07","on":"CI on PR 63","until":"2026-11-07T09:00:00+01:00","reason":"needs green CI"}
```

Send it to `workflow <project-dir> wait -`. `until` and `reason` are optional; `reason` falls back
to the task's `block_reason`.

Resume, context and the dashboard show the wait. Once `until` passes, `move_detail` says
`wait due`. The wait ends when the task leaves BLOCKED, and only a wait recorded after the task's
latest `task block` counts. Block with `task block`: a re-block through `update` or `reconcile`
writes no anchor, so an older wait can reappear. Record a new `wait` to replace it.

## Reusing a Goldfish round

After an editorial-only change, `goldfish waive` may name the round it relies on with
`"reuses": NN`, together with the user's `confirmation`. Round NN must be `accepted` or
`accepted-residual` (not a waiver) and must have the same target and worktree. The default stays a
fresh round.

## What the counts rely on

Crash loops and waits are read from `invocations.jsonl`, which the launcher appends after each
command. A failed append is silent, so a missing line can hide a wait or miss a resume. Treat the
counts as a safety net, not as proof.
