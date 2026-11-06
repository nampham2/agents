# Repository worktrees

Every repository a project writes to gets its own git worktree and a draft merge request (MR; a
GitHub pull request or GitLab merge request). Both are recorded in `project.json` before the
first write. The agent runs Git and host commands only after the user confirms them.

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
   `number`. A repository with no remote records `"exempt":"no_remote"` instead; a project never
   pushed records `"exempt":"local_only"`, with the user's words. A non-repository target needs
   neither.
7. Record an alpha line only for a test or experiment build, and the version decision before
   closing ([versioning.md](versioning.md)).

Delivery order: release, merge on the host, record the merge, then close/remove. Removal is last and
refused while the release or merge is unrecorded.

Every record takes `path` and a quoted `confirmation`; `workflow <project-dir> worktree --schema`
prints each operation's keys.

After the gate cutoff, `task start` refuses a task with `target` outputs until step 6 is recorded.

## Never

Create, commit, push, merge or remove without the user's words for it. The
research workspace root is no project repository: no worktree, no MR.
