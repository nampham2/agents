# Maintenance and exceptional states

Use `BLOCKED` when progress needs input, authority, or external state; record the actual blocker.
For cancellation, stop running work, resolve ownership, set `CANCELLED` and `cancellation_reason`,
and preserve partial outputs and evidence without claiming completion.

To maintain a completed deliverable, validate its baseline, transition `DONE → PLANNING`, and
append tasks with new IDs. Keep terminal tasks, previous reviews and decisions. A successor is
appropriate for a materially different objective or ownership.

Use [workspace-schema.md](workspace-schema.md) only for exact fields/transitions, migration or
unusual repairs. `migrate` previews by default; applying migration needs explicit authorization.
Never manufacture consent from old running work. v3 already supports ordinary sequential work
and native workers; it does not need migration to use the lean lifecycle.
