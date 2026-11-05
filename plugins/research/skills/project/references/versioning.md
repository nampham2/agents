# Versions, the alpha line and the release

Read this before proposing a base, recording an alpha line, moving a base or recording a release.
`workflow <project-dir> worktree` records each step.

A repository project closes with a release record or an exemption (see Close). An alpha line is
optional: record one only for a build that differs from the release (test deploy, plugin refresh,
experiment).

## Pick a base

1. Find the version files. Python files and plugin manifests hold `X.Y.Z`.
2. With the user's words, run `git fetch --prune`; the tool reads local refs only.
3. Propose a base that main has not released and that no other live project, `origin/*` branch,
   local worktree or local branch holds. The tool refuses an occupied base and suggests the
   patch above the highest version seen; occupancy rules are in
   [workspace-schema.md](workspace-schema.md).

### Read a `uv.lock` at its own entry

The tool reads only the project's own entry in a `uv.lock`: the `[[package]]` block whose `source`
is `{ editable = "." }` or `{ virtual = "." }`. This holds for the occupancy, alpha and
release checks. A lock with no such entry states no version.

## Record an alpha line (optional)

1. After the user confirms the base, write `X.Y.ZaN` into Python files and `X.Y.Z-alpha.N` into
   plugin manifests.
2. Record `"operation":"alpha"` with `files` (`path`, `style` `pep440` or `semver`), `base`,
   `alpha` and `reason`.
3. Keep one alpha line per base. Record `"operation":"alpha"` again before each test deploy or
   plugin refresh, raising only the number.
4. If main released past the base, or the tool refuses it as occupied, ask the user. Then send
   the new `base` with `"base_moved":true`, a `reason` and `"alpha":1`; a moved base restarts the
   number at 1.

## Close

Before `finalize`, a repository project needs one of:

- A release record. Write the plain `X.Y.Z` into each version file. Record `"operation":"release"`
  with `version`. With no alpha line, also send `files` (`path`, `style`); the release then starts
  a release-only line.
- An exemption. If the user agrees, record `"operation":"alpha"` with `"exempt":true`, a
  `reason` and their words. Use it for a repository with no version file or a change needing no
  new version. An exemption and a release record exclude each other, except that a release with
  `"replaces_exemption":true`, `version`, `files` and the user's words replaces the exemption; the
  exemption moves to `version_exemption_replaced` with the release confirmation beside it.
