# Write a checkable handoff

Use this reference whenever writing continuation fields or a companion brief, including closure.
`handoff.md` is the authoritative continuation note; it links canonical state, authorization and
evidence rather than replacing them. A brief such as `NEXT-SESSION.md` is subordinate to it.

## Claims and observations

For mutable external facts the successor will rely on, put provenance beside the claim: the exact
target, observed result, observation time with timezone, and an evidence pointer or retained output.
Include the read-only command or concrete lookup that would re-check it. Derive current state from
the system concerned: local Git cannot establish a PR's state, and a receipt cannot establish
current service health. Distinguish the time of an event from the time it was observed.

If not checked, unavailable, or contradicted, mark the claim `UNVERIFIED` inline and give the
re-check and any dependency it blocks. A previously observed result remains historical evidence;
do not silently promote it to current truth when refreshing a note. The checkpoint's `recorded`
time dates its save, not each observation. For example, with the actual repository filled in:

```text
PR #5: UNVERIFIED; re-check gh pr view 5 --repo OWNER/REPO --json state,mergedAt,mergeCommit
before relying on merge status. The local branch/commit does not settle this.
```

When checked, replace that line with the result, observation time and evidence pointer while
retaining the re-check. Keep tool-derived metadata distinct from agent-authored claims.
[Freshness](automation-context.md) compares checkpoint revision, spec/design hashes and local Git;
matching hashes do not establish undeclared/remote freshness. Re-check consequential remote claims
before dependent work, even when local freshness is unchanged.

## Verifiers and constraints

Whenever handing over a verifier, state its command, intended exit meanings (including pending),
and observed test status. Link evidence for the script version, inputs/environment, exercised
branches and results. A pending run does not exercise the completed success or failure branch.
Prefer safe fixtures, a controlled clock or shortened input to exercise terminal branches; say
what that test establishes. A shortened test does not satisfy the real observation window or
authorize the gated action.

For an unexercised path say, for example: `UNVERIFIED: exit-0 terminal path unexercised; only
pending exit 3 observed.` Explain how to distinguish a subject failure from a checker fault. A
traceback, missing dependency or malformed output is a checker fault with no subject verdict;
exit 1 alone may not distinguish the two. On an ambiguous failure, inspect retained output and
check the verifier before diagnosing the subject or restarting an observation window. Keep gated
actions blocked until a valid passing verdict and the required authorization both exist.

Keep a literal `## Do not` list of applicable prohibitions, or explicitly state none. For each,
name the reason and the observable condition that would show a violation, plus the evidence or
authorization needed to lift it. State what is authorized and what remains unauthorized with
source pointers; neither a passing check nor a handoff grants permission.

## Next action, briefs and replacement

Start `## Next` with `workflow <absolute-project-dir> resume -` and JSON `{}` on stdin, using the
resolved launcher. Inspect its freshness findings before the task; `resume` already includes this
check. For a targeted re-check use `workflow <absolute-project-dir> freshness -` with `{}`. Follow
with the external re-checks relevant to the next action and exact instants with timezones.

If a companion brief exists, include the absolute handoff path, its saved SHA-256/revision, and a
`Valid until` instant with timezone or an explicit invalidation condition. Require resume before
acting; expiry or a changed handoff token means discard the brief's instructions and reload the
handoff. Refresh or replace the brief with a closure notice when closing; avoid duplicating task
counts or mutable remote claims.

Before replacing a handoff or brief, preserve consequential superseded claims and corrections in
owning records with their sources. When no recoverable history exists, retain a dated snapshot of
the old note in the project before overwriting it. Report preservation failures; never assume
version control or initialize/commit a repository implicitly.

Before yielding, inspect each actionable claim for provenance or `UNVERIFIED`, each verifier for
branch evidence or an explicit gap, and each prohibition for its observable guard. Search the
note/brief with `rg -n 'UNVERIFIED' <paths>` to find pending re-checks; zero hits do not prove
completeness or truth of the claims.
