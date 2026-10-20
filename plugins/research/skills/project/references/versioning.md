# Versions and the alpha line

Read this before proposing a base, recording an alpha line, moving a base or recording a release.
`workflow <project-dir> worktree` records each step. The tool checks the files in the worktree.

## Pick a base

1. Find the version files. Python files hold `X.Y.Z`; plugin manifests hold `X.Y.Z`.
2. With the user's words, run `git fetch --prune`. The tool reads local refs only.
3. Propose a base that main has not released and that no other live project, `origin/*` branch,
   local worktree or local branch holds. The tool refuses an occupied base, names each occupier
   and suggests the patch above the highest version seen. The occupancy rules are in
   [workspace-schema.md](workspace-schema.md).

## Record the alpha line

1. After the user confirms the base, write `X.Y.ZaN` into Python files and `X.Y.Z-alpha.N` into
   plugin manifests.
2. Record `"operation":"alpha"` with `files` (`path`, `style` `pep440` or `semver`), `base`,
   `alpha` and `reason`. With no version file, record `"exempt":true`.
3. Keep one alpha line per base. Record `"operation":"alpha"` again before each test deploy or
   plugin refresh. While the base stays, increase only the number.
4. If main released past the base, or the tool refuses it as occupied, ask the user. Then send
   the new `base` with `"base_moved":true`, a `reason` and `"alpha":1`. A moved base restarts the
   number at 1.
