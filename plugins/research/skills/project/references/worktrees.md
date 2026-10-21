# Repository worktrees

Every repository a project writes to gets its own git worktree and a draft merge request (MR).
Both are recorded in `project.json` before the first write. MR: a GitHub pull request or a GitLab
merge request. The tools observe Git and record what they see. The agent runs Git and host
commands only after the user confirms them in their own words.

## Before the first write

1. `task ... start` and `workflow ... resume` return `worktree`. A warning means the target is a
   repository (or linked worktree) with no record; legacy projects warn the same way.
2. Propose, do not assume. Path: `<repo-parent>/<repo-name>.worktrees/<project-id>`. Branch:
   `<prefix>/<project-id>-<slug>`, with the prefix the repository's local branches already use,
   else `project/`, and `<slug>` from the title. Also propose the empty seed commit, the push, the
   draft MR and one approval to post replies and resolve threads on that MR. Keep the user's
   confirming words for the record.
3. Create it with `git -C <repo> worktree add -b <branch> <path>`. `git status --short --branch` in
   the main checkout must read the same before and after.
4. Run `git commit --allow-empty`, `git push -u origin <branch>`, then `gh pr create --draft`
   (GitHub) or `glab mr create --draft` (GitLab). Read the host from `git remote get-url origin`.
   If the host is unknown or the CLI is not logged in, stop and ask. List the branch's MRs
   before a retry.
5. Record the worktree:

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
   worktree; `"kind":"none"` records once that the target is not a repository. The tool refuses a
   mismatched path, branch or repository: fix the worktree, not the request.
6. Record the MR with `"operation":"pull_request"`, `host` (`github` or `gitlab`), `url` and
   `number`. A repository with no remote records `"exempt":"no_remote"` instead. A target that is
   no repository needs neither.
7. Record an alpha line only when the project needs a test or experiment build, and the version
   decision before closing, as [versioning.md](versioning.md) says.

Every record above takes `path` and a quoted `confirmation`; `workflow <project-dir> worktree
--schema` prints each operation's full key list.

For a project created after the gate cutoff, `task start` refuses a task with `target` outputs
until step 6 is recorded.

## Never

Create, commit, push, merge or remove without the user's words for that action. The
research workspace root is no project repository: it needs no worktree and no MR.
