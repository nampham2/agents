# Repository worktrees

Every repository a project writes to gets its own git worktree, recorded in `project.json` before
the first write. The tools observe Git and record what they see; the agent runs the Git commands,
and only after the user has confirmed them in their own words.

## Before the first write

1. `task ... start` and `workflow ... resume` return `worktree`. A warning means the target is a
   repository (or linked worktree) with no record; legacy projects warn the same way.
2. Propose, do not assume. Path: `<repo-parent>/<repo-name>.worktrees/<project-id>`. Branch:
   `<prefix>/<project-id>-<slug>`, with the prefix the repository's local branches already use (for
   example `npham/`), else `project/`, and `<slug>` from the title. Show both, let the user confirm
   or edit, and keep their words for the record.
3. Create it with `git -C <repo> worktree add -b <branch> <path>`. `git status --short --branch` in
   the main checkout must read the same before and after.
4. Record it:

   ```json
   {"id":"worktree-target","expected_revision":N,"tokens":{"handoff":"<sha>"},
    "operation":"record","role":"target","path":"<path>","branch":"<branch>",
    "repository":"<repo>","confirmation":{"source":"user reply <date>","response":"<quoted>"},
    "continuation":{"next":"..."}}
   ```

   `workflow <project-dir> worktree -` checks that the path is a linked worktree of the repository
   on that branch, stores its head as `base_commit`, and repoints `working_directory` (the `target`
   root) to it; use absolute paths afterwards. Repeat with `"role":"additional"` for each further
   repository (these do not repoint). `"kind":"existing"` records a target that already is a linked
   worktree; `"kind":"none"` records once that the target is not a repository.
5. A mismatched path, branch or repository is refused and nothing is written: fix the worktree, not
   the request.

## Before closing

1. For each recorded worktree show the user `git status --short --branch` (`workflow ... readiness`
   lists findings) and ask whether to commit now (only when asked, never implicitly) and whether to
   keep or remove it.
2. Record the answer:

   ```json
   {"id":"worktree-close","expected_revision":N,"tokens":{"handoff":"<sha>"},
    "operation":"close","path":"<path>","decision":"keep",
    "confirmation":{"source":"user reply <date>","response":"<quoted>"}}
   ```

   `keep` requires a clean tree. `accept_dirty` stores the dirty paths beside the user's explicit
   acceptance, and covers only those paths: a later path is a closure finding until accepted again.
   `remove` is destructive: with the user's authorization the agent runs
   `git worktree remove <path>` (and `git branch -d <branch>` only if asked) first; the tool then
   verifies the worktree is gone and points the target back at the repository.
   A kept entry can be decided again (`keep`, `accept_dirty` or `remove`); the earlier decision is
   saved to the spec's decision history, so include the `spec` token. A removed entry is final.
3. `finalize`, `close` and an `update` to DONE refuse an undecided worktree, or one kept as clean
   that is dirty again. Without Git the guard degrades to warnings: mark those worktrees
   `UNVERIFIED` in the handoff with the re-check command.

## Never

Create, commit, push or remove without the user's words for that specific action, and never treat
the research workspace root as a project repository: it is exempt.
