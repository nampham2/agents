# Merge requests and delivery

Read this before a review ask, a test deploy and repository closure. An MR means a GitHub pull request or a GitLab merge request.

## Before asking for review or deploying a test

1. Read every unresolved thread, human and bot, on each recorded MR. GitHub: `gh api
   graphql` with `reviewThreads` and `gh pr view --comments`. GitLab: `glab mr view --comments`.
2. Fix or answer each thread. Commit and push fixes only as the user approved; post
   replies and resolve threads under the worktree confirmation's approval.
3. Save one finding listing each thread and outcome. Sweep again before the next ask.
4. If a thread cannot be read, mark the sweep `UNVERIFIED` with its re-check command. Do not ask
   or deploy; tell the user.

## Release and merge

The delivery order is fixed: release, merge on the host,
record the merge, then close/remove.

1. Sweep the threads.
2. Commit the plain release version `X.Y.Z` into each version file and push, then run the diff
   Goldfish round ([goldfish-review.md](goldfish-review.md)). Record `"operation":"release"`
   with `version` ([versioning.md](versioning.md)). If main
   released past the base, ask the user, then add `"base_moved":true` and a `reason`.
3. Run `readiness` with `required_checks` (host-required); fix each `checks.missing` name. Ask
   the user to merge each MR by `squash`, `merge` or `rebase`.
4. After a yes, merge on the host; a `local_only` worktree merges in the main checkout, with the
   commit main then points to as `evidence`. Record `"operation":"merge"` with
   `"merge_state":"merged"`, `method`, `evidence` (merge commit or URL), `merged_head` (the
   merged reviewed head, full hex) and the user's words.
5. After a no, record `"merge_state":"declined"` with the user's words; the worktree stays.

An alpha version never merges to main.

## Clean up

1. Ask only after the merge is recorded.
2. Run `workflow ... readiness`; record each worktree's `missing` delivery records. Show
   `git status --short --branch`; ask whether to commit (only when asked), keep or remove.
3. A `no_remote` or `local_only` worktree needs a current diff Goldfish round before its first
   closure and every remove; a declined local merge needs none, and with its branch gone its latest
   passed round stands. Record the answer:

   ```json
   {"id":"worktree-close","expected_revision":N,"tokens":{"handoff":"<sha>"},
    "operation":"close","path":"<path>","decision":"keep",
    "confirmation":{"source":"user reply <date>","response":"<quoted>"}}
   ```

   `keep` requires a clean tree. `accept_dirty` covers only the dirty paths stored with the
   user's acceptance. `remove` is destructive: with the user's authorization run `git worktree
   remove <path>` first (`git branch -d <branch>` only if asked). A kept entry can be decided
   again with the `spec` token; removal is final.
4. `finalize` refuses an undecided or dirty worktree. Without Git, mark the worktree `UNVERIFIED`.
5. If a project is cancelled with an open MR, ask whether to close it.

## Commit the project

Do this after `finalize` and its report.

1. Run `git -C <workspace-root> status --short`. Stop if a path outside the project folder
   changed or the workspace root is no repository.
2. Show the file list and message; wait for a yes.
3. Run `git -C <workspace-root> add -- <project-dir>`, then `git -C <workspace-root> commit -m
   "<message>" -- <project-dir>`. No push unless asked.
4. Report the commit hash. Do not edit the closed project.
