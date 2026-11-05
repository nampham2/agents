# Write a checkable handoff

Use this reference when writing continuation fields or a companion brief, including at closure.
`handoff.md` is the authoritative continuation note and links canonical state, authorization and
evidence. A brief such as `NEXT-SESSION.md` is subordinate.

## Claims and observations

For a mutable external fact the successor relies on, put provenance beside the claim: the exact
target, observed result, observation time with timezone, an evidence pointer, and the read-only
command that re-checks it. Derive current state from the system concerned: local Git cannot show a
PR's state, and a receipt cannot show service health.

If a claim is unchecked, unavailable or contradicted, mark it `UNVERIFIED` inline with the re-check
and the dependency it blocks. An earlier observation stays historical; do not promote it
to current truth. `recorded` dates the save, not each observation. Example:

```text
PR #5: UNVERIFIED; re-check gh pr view 5 --repo OWNER/REPO --json state,mergedAt,mergeCommit
before relying on merge status. The local branch/commit does not settle this.
```

When checked, replace that line with the result, observation time and evidence pointer, keeping the
re-check.
[Freshness](automation-context.md) compares revision, spec/design hashes and local Git; matching
hashes do not show remote freshness, so re-check consequential remote claims.

## Verifiers and constraints

When handing over a verifier (a script, check or observation window), read
[handoff-verifiers.md](handoff-verifiers.md) first.

Put prohibitions, or a statement of none, in `do_not`: it renders as the first `## Do not`
section and `resume` returns it. For each, give the reason, the observable
condition that shows a violation, and the evidence or authorization that lifts it. State what is
authorized, with source pointers; a passing check or handoff grants no permission.

## Next action, briefs and replacement

Start `## Next` with `workflow <absolute-project-dir> resume -` and JSON `{}` on stdin, using the
resolved launcher, and inspect its freshness findings first (`workflow <absolute-project-dir>
freshness -` with `{}` re-checks alone). Then list the external re-checks the next action needs,
with instants and timezones.

A companion brief names the absolute handoff path, its saved SHA-256/revision, and a `Valid until`
instant with timezone or an invalidation condition. Require resume before acting; on expiry or a
changed token discard the brief and reload the handoff. Replace the brief
with a closure notice on closing. Do not copy task counts or mutable remote claims.

Before replacing a handoff or brief, preserve consequential superseded claims and corrections in
owning records with sources; with no recoverable history, keep a dated snapshot of the old note.
Report preservation failures. Never assume version control or initialize or commit a repository.

Before yielding, check each claim for provenance or `UNVERIFIED`, each verifier for branch evidence
or an explicit gap, and each prohibition for its observable guard. `rg -n
'UNVERIFIED' <paths>` finds pending re-checks; zero hits do not prove the claims true.
