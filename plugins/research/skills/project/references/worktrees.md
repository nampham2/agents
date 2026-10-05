# Repository worktrees

Every repository a project writes to gets its own git worktree and a draft merge request (MR).
Both are recorded in `project.json` before the first write. An MR means a GitHub pull request or
a GitLab merge request. The tools observe Git and record what they see. The agent runs the Git and
host commands, and only after the user has confirmed them in their own words.

## Before the first write

1. `task ... start` and `workflow ... resume` return `worktree`. A warning means the target is a
   repository (or linked worktree) with no record; legacy projects warn the same way.
2. Propose, do not assume. Path: `<repo-parent>/<repo-name>.worktrees/<project-id>`. Branch:
   `<prefix>/<project-id>-<slug>`, with the prefix the repository's local branches already use (for
   example `npham/`), else `project/`, and `<slug>` from the title. Also propose the empty seed
   commit, the push, the draft MR, the alpha line (step 7) and one approval to post replies and
   resolve threads on that MR. Let the user confirm or edit, and keep their words for the record.
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
   mismatched path, branch or repository and writes nothing: fix the worktree, not the request.
6. Record the MR with `"operation":"pull_request"`, `host` (`github` or `gitlab`), `url` and
   `number`. A repository with no remote records `"exempt":"no_remote"` instead. A target that is
   no repository needs neither.
7. Find the version files. Propose a base `X.Y.Z` and a first alpha `X.Y.Za1`. After the user
   confirms, write `X.Y.ZaN` into Python files and `X.Y.Z-alpha.N` into plugin manifests. Record
   `"operation":"alpha"` with `files` (`path`, `style` `pep440` or `semver`), `base`, `alpha` and
   `reason`. With no version file, record `"exempt":true`.
8. Keep one alpha line. Record `"operation":"alpha"` again before each test deploy or plugin
   refresh. Increase only the number. Never change the base.

For a project created after the gate cutoff, `task start` refuses a task with `target` outputs
until steps 6 and 7 are recorded.

## Never

Create, commit, push, merge or remove without the user's words for that specific action. The
research workspace root is no project repository: it needs no worktree and no MR.
