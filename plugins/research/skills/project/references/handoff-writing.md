# Write a checkable handoff

Use this reference whenever writing continuation fields or a companion brief, including closure.
`handoff.md` is the authoritative continuation note; it links canonical state, authorization and
evidence rather than replacing them. A brief such as `NEXT-SESSION.md` is subordinate to it.

## Claims and observations

For mutable external facts the successor will rely on, put provenance beside the claim: the exact
target, observed result, observation time with timezone, and an evidence pointer, plus the read-only
command that would re-check it. Derive current state from the system concerned: local Git cannot
establish a PR's state, and a receipt cannot establish current service health. Distinguish when an
event happened from when it was observed.

If a claim was not checked, is unavailable or is contradicted, mark it `UNVERIFIED` inline with the
re-check and the dependency it blocks. An earlier observation stays historical evidence; do not
promote it to current truth when refreshing a note. The checkpoint's `recorded` time dates the
save, not each observation. For example:

```text
PR #5: UNVERIFIED; re-check gh pr view 5 --repo OWNER/REPO --json state,mergedAt,mergeCommit
before relying on merge status. The local branch/commit does not settle this.
```

When checked, replace that line with the result, observation time and evidence pointer, keeping the
re-check. Keep tool-derived metadata distinct from agent-authored claims.
[Freshness](automation-context.md) compares checkpoint revision, spec/design hashes and local Git;
matching hashes do not establish remote freshness, so re-check consequential remote claims before
dependent work.

## Verifiers and constraints

When handing over a verifier (a script, check or observation window), read
[handoff-verifiers.md](handoff-verifiers.md) first. Otherwise this section needs nothing more.

Put the applicable prohibitions, or an explicit statement of none, in `do_not`: it renders as the
first `## Do not` section and `resume` returns it. For each, give the reason, the observable
condition that would show a violation, and the evidence or authorization needed to lift it. State
what is authorized and what is not, with source pointers; neither a passing check nor a handoff
grants permission.

## Next action, briefs and replacement

Start `## Next` with `workflow <absolute-project-dir> resume -` and JSON `{}` on stdin, using the
resolved launcher, and inspect its freshness findings first (`workflow <absolute-project-dir>
freshness -` with `{}` re-checks alone). Follow with the external re-checks the next action needs
and exact instants with timezones.

A companion brief names the absolute handoff path, its saved SHA-256/revision, and a `Valid until`
instant with timezone or an invalidation condition. Require resume before acting; on expiry or a
changed handoff token discard the brief's instructions and reload the handoff. Replace the brief
with a closure notice when closing, and do not duplicate task counts or mutable remote claims.

Before replacing a handoff or brief, preserve consequential superseded claims and corrections in
owning records with their sources; with no recoverable history, keep a dated snapshot of the old
note in the project. Report preservation failures, and never assume version control or initialize
or commit a repository implicitly.

Before yielding, check each actionable claim for provenance or `UNVERIFIED`, each verifier for
branch evidence or an explicit gap, and each prohibition for its observable guard. `rg -n
'UNVERIFIED' <paths>` finds pending re-checks; zero hits do not prove the claims complete or true.
