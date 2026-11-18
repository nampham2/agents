# Review risk, fallback, amendments and options

Policy 3+ projects only. Policies 0 to 2 keep the effort rules in
[goldfish-review.md](goldfish-review.md). `workflow <project-dir> settings` is the one writer of
`review_risk`, `review_fallback` and `options`. It needs the user's `confirmation` (`source`,
`response` quoting their words) and records one dated decision line. `null` unsets a field;
`options` merge per key. Change a value only on the user's words, in any status but `DONE` and
`CANCELLED`.

## Review risk

The coordinator proposes a level at planning and the user confirms it with the planning
confirmation. Write down three reasons: consequence if wrong, reversibility, interface change.
`high` when any reason is strong (a costly failure, hard to undo, or a change to an interface
others rely on); `low` when the plan effort is low and no reason is strong; otherwise `standard`.
`high` wins over low effort. Unset reads as `standard`.

| Risk | Spec and planning | Design and diff | Re-audit forced by |
| --- | --- | --- | --- |
| `low` | one `combined` | one `combined` | a Blocker, at every target |
| `standard` | one `combined` | one `combined` | Blocker (spec, planning); Blocker or Major (design, diff) |
| `high` | one `combined` | `comprehension`, `design-critic`, `zoom-out`, `readiness` | as `standard` |

The tool refuses any other stage list. A delta or re-audit round follows the same table. The risk
at `close` decides the outcome, and a design or diff round opened below `high` cannot close once
the risk is `high`: open a new round. At `low`, a Major in a design or diff round neither forces a
re-audit nor needs an `artifact` for a `FIX` on an unchanged target; it closes as `accepted` with
no confirmation, unless it is contested or escalated.

## Standing fallback

For a host with no fresh agents, record once at alignment `review_fallback`:
`{"mode": "waive-recorded", "reason": "<why>"}`. The default is `{"mode": "ask"}`: ask each time.

With `waive-recorded`, a spec or planning waiver below `high` needs no `reason` or `confirmation`.
The round quotes the stored reason and says the coordinator asserted that no fresh agent was
available; the tool cannot check that. At `high` the standing reason is ignored. A design or diff
waiver always needs its own `confirmation`. `validate` warns while a standing waiver stands in for
a review, until a later round passes.

## Amendment

After a passing close, an explicit edit to the spec or planning needs no new round when it is
editorial or a reviewer asked for it. Run `workflow goldfish` with `operation: amend`, `target`
(`spec` or `planning`), `kind` (`editorial` or `reviewer-requested`) and `reason`. The tool reads
the current text itself and writes a new round that reuses the last passing one. It refuses design
and diff, a target with no passing review, text that matches the reviewed round, and a target
holding any waiver that is not an amendment since its last review. It needs the user's
`confirmation` (`source`, `response`; the coordinator supplies the words, so the tool cannot check
them) in three cases: the review risk is `high`; the project's `review_fallback` is not
`waive-recorded`; or an amendment already stands since the last review. Below `high` with a
`waive-recorded` fallback, only the first amendment needs no words. That limit constrains `amend`
only: a standing waiver also needs no words and can repeat, by design. Use `waive` with `reuses`
for a reuse record without an edit kind.

An amendment is your attestation that the edit is not substantive. The confirm result lists every
amendment since the last review under `amendments`; show them to the user. `validate` warns until
a round passes. Any edit with no matching amendment still needs a round. Use a round for a
substantive change.

## Options

| Option | Default | When off |
| --- | --- | --- |
| `style_advice` | on | style findings stop |
| `memory_staging` | on | `finalize` stages no lessons and warns of none |
| `reports` | off | the `report` action returns a notice and writes nothing |
| `delegating` | off | policy 5 only; the packet adds no dependency or hash warnings |

Other keys are refused, an empty object too. `delegating` below policy 5 is refused with
`options.delegating needs policy_version 5`. Set a key back to its default by writing that
value; `null` unsets all options. A fallback of `ask` takes no reason. At policy 3+ the `finalize`
control warning applies only when a code task is running or done.

## Delegating mode

A boolean option from policy 5, off by default. It governs two packet warnings only; see
[task-workers.md](task-workers.md).
