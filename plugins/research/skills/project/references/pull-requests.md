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

Do this once, when all the work is done.

1. Sweep the threads.
2. Commit the plain release version `X.Y.Z` into each recorded version file and push. Record
   `"operation":"release"` with `version`. If the base release moved on the main branch, stop and
   ask the user.
3. Ask the user to merge each MR and name the method: `squash`, `merge` or `rebase`.
4. After a yes, merge on the host. Record `"operation":"merge"` with `"merge_state":"merged"`,
   `method`, `evidence` (the merge commit or URL) and the user's words.
5. After a no, record `"merge_state":"declined"` with the user's words. The worktree stays.

An alpha version never merges to the main branch. A worktree with no MR needs no merge.

## Clean up

1. Ask only after the merge is recorded. The tool refuses removal while an MR is not merged.
2. For each recorded worktree show `git status --short --branch` (`workflow ... readiness` lists
   findings). Ask whether to commit now (only when asked) and whether to keep or remove it.
3. Record the answer:

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
5. If a project is cancelled with an open MR, ask the user whether to close the MR.

## Commit the project

Do this after `finalize` and a report of the result.

1. Run `git -C <workspace-root> status --short`. If a path outside the project folder changed,
   stop and ask the user. If the workspace root is no repository, say so and stop.
2. Show the file list and a commit message. Wait for the user's yes.
3. Run `git -C <workspace-root> add -- <project-dir>`, then `git -C <workspace-root> commit -m
   "<message>" -- <project-dir>`. Do not push unless the user asks.
4. Report the commit hash. Do not edit the closed project afterwards.
