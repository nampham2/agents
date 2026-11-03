# Merge requests and delivery

Read this before the agent asks the user for review, before it deploys a test, and when it
closes a repository project. An MR means a GitHub pull request or a GitLab merge request.

## Before asking for review or deploying a test

1. Read every unresolved thread on each recorded MR, from people and from bots. GitHub: `gh api
   graphql` with `reviewThreads` and `gh pr view --comments`. GitLab: `glab mr view --comments`.
2. Fix the code or answer each thread. Commit and push fixes only as the user approved. Post
   replies and resolve threads under the approval from the worktree confirmation.
3. Save one finding that lists each thread and its outcome. Sweep again before the next ask or
   deploy.
4. If a thread cannot be read, mark the sweep `UNVERIFIED` with its re-check command. Do not ask
   for review or deploy. Tell the user.

## Release and merge

The delivery order is fixed: release, merge on the host,
record the merge, then close/remove.

1. Sweep the threads.
2. Commit the plain release version `X.Y.Z` into each version file and push, then run the diff
   Goldfish round ([goldfish-review.md](goldfish-review.md)). Record
   `"operation":"release"` with `version` (see [versioning.md](versioning.md)). If the base
   release moved on the main branch, ask the user, then add `"base_moved":true` and a `reason`.
3. Run `readiness` with `required_checks` (host-required); fix each `checks.missing` name first.
   Ask the user to merge each MR by `squash`, `merge` or `rebase`.
4. After a yes, merge on the host; a `local_only` worktree merges in the main checkout and its
   `evidence` is the commit main then points to. Record `"operation":"merge"` with
   `"merge_state":"merged"`, `method`, `evidence` (the merge commit or URL), `merged_head` (the
   reviewed head that was merged, full hex) and the user's words. The tool checks `merged_head`
   against the diff round, and against the local branch head while that branch exists.
5. After a no, record `"merge_state":"declined"` with the user's words. The worktree stays.

An alpha version never merges to the main branch.

## Clean up

1. Ask only after the merge is recorded.
2. Run `workflow ... readiness`; record each worktree's `missing` delivery records first. A
   gone directory is refused with its `git worktree add` fix. Show `git status --short --branch`;
   ask whether to commit now (only when asked), keep or remove.
3. A `no_remote` or `local_only` worktree needs a current diff Goldfish round before its first
   closure and before every remove; a declined local merge needs none, and its removal names the
   abandoned commits; with its branch gone, its latest passed round stands. Record the answer:

   ```json
   {"id":"worktree-close","expected_revision":N,"tokens":{"handoff":"<sha>"},
    "operation":"close","path":"<path>","decision":"keep",
    "confirmation":{"source":"user reply <date>","response":"<quoted>"}}
   ```

   `keep` requires a clean tree. `accept_dirty` stores the dirty paths beside the user's
   acceptance and covers only those paths. `remove` is destructive: with the user's authorization
   run `git worktree remove <path>` (and `git branch -d <branch>` only if asked) first. A kept
   entry can be decided again; include the `spec` token. A removed entry is final.
4. `finalize` refuses an undecided or dirty worktree and, after the gate cutoff, a missing MR,
   merge decision or version record. Without Git, mark the worktree `UNVERIFIED`.
5. If a project is cancelled with an open MR, ask whether to close it.

## Commit the project

Do this after `finalize` and its report.

1. Run `git -C <workspace-root> status --short`. If a path outside the project folder changed,
   or the workspace root is no repository, stop and say so.
2. Show the file list and message; wait for the user's yes.
3. Run `git -C <workspace-root> add -- <project-dir>`, then `git -C <workspace-root> commit -m
   "<message>" -- <project-dir>`. Do not push unless the user asks.
4. Report the commit hash. Do not edit the closed project afterwards.
