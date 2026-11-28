# Repository worktrees

Every repository a project writes to gets its own git worktree, recorded in `project.json` before
the first write. What else it needs depends on the project's delivery target (below); where the
target demands one, a draft merge request (MR; a GitHub pull request or GitLab merge request) too.
The agent runs Git and host commands only after the user confirms them.

## Delivery target

A project created from 0.38.0 starts at `policy_version` 2 and records its `delivery_target` with
the alignment confirmation (`delivery_target` on `workflow confirm`, kind `alignment`); it cannot
leave `ALIGNING` without one. `workflow <project-dir> retarget` changes it later, with the user's
words and a reason, and lists the obligations gained and dropped. Ask the user which target fits:

| Target | Before a write to the target | At closure |
| --- | --- | --- |
| `research` | refused: it writes nothing to the target; no worktree warning | each recorded worktree still needs its keep/remove decision |
| `local` | record the worktree (steps 1-3, 5); no MR, so skip 4 and 6 | keep/remove decision |
| `pr` | draft MR or `no_remote` exemption | keep/remove decision and a diff Goldfish round at closure (a recorded merge excuses it); no merge or release |
| `merged` | draft MR or `no_remote` exemption | merge record or decision, and the diff round at the merge |
| `release` | as `merged` | as `merged`, plus a release record or version exemption and the diff round at the release |

A project from before 0.38.0 keeps `policy_version` 0 (owes none of this) or 1 (owes what `release`
owes, plus the diff round for a `no_remote` or `local_only` worktree). `research-project
migrate-policy <project-dir> --to 2 --target <target>` previews the move and writes nothing;
`--apply --expected-revision N --confirm "<user's words>"` applies it and keeps existing records.

## Before the first write

1. `task ... start` and `workflow ... resume` return `worktree`; a warning means an unrecorded
   target repository or linked worktree.
2. Propose, do not assume. Path: `<repo-parent>/<repo-name>.worktrees/<project-id>`. Branch:
   `<prefix>/<project-id>-<slug>` (prefix: what local branches use, else `project/`; `<slug>`
   from the title). Also propose the empty seed commit, the push, the
   draft MR and one approval to post replies and resolve threads on it. Keep the user's words.
3. Create it with `git -C <repo> worktree add -b <branch> <path>`. `git status --short --branch` in
   the main checkout must read the same before and after.
4. Run `git commit --allow-empty`, `git push -u origin <branch>`, then `gh pr create --draft`
   (GitHub) or `glab mr create --draft` (GitLab). Read the host from `git remote get-url origin`;
   if it is unknown or the CLI is not logged in, stop and ask. List the branch's MRs before a retry.
5. Record it:

   ```json
   {"id":"worktree-target","expected_revision":N,"tokens":{"handoff":"<sha>"},
    "operation":"record","role":"target","path":"<path>","branch":"<branch>",
    "repository":"<repo>","confirmation":{"source":"user reply <date>","response":"<quoted>"},
    "continuation":{"next":"..."}}
   ```

   `workflow <project-dir> worktree -` stores the head as `base_commit` and repoints
   `working_directory` (the `target` root) to the worktree; use absolute paths after. Repeat
   with `"role":"additional"` per further repository (no repoint).
   `"kind":"existing"` records a target that already is a linked worktree; `"kind":"none"` records
   a non-repository target once. Fix refusals in the worktree, not the request.
6. Record the MR with `"operation":"pull_request"`, `host` (`github` or `gitlab`), `url` and
   `number`. A repository with no remote records `"exempt":"no_remote"` instead. A project never
   pushed uses target `local`, which needs no MR; only policy 0 and 1 projects still record
   `"exempt":"local_only"` (with the user's words), and a legacy `local_only` entry stays valid;
   replaces_exemption replaces it.
   A non-repository target needs neither.
7. Record an alpha line only for a test or experiment build, and the version decision before
   closing ([versioning.md](versioning.md)).

Delivery order: release, merge on the host, record the merge, then close/remove. Removal is last and
refused while the release or merge is unrecorded.

Every record takes `path` and a quoted `confirmation`; `workflow <project-dir> worktree --schema`
prints each operation's keys.

For targets `pr`, `merged` and `release` (and policy 1), `task start` refuses a task with `target`
outputs until step 6 is recorded. Target `research` refuses such a task whether or not a worktree
exists; target `local` does not need step 6.

## Never

Create, commit, push, merge or remove without the user's words for it. The
research workspace root is no project repository: no worktree, no MR.
