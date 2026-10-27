# agents

Cross-host research plugins for Claude Code, Codex and Pi.

## Install through a marketplace

Plugins are distributed through the repository marketplace for each host. Do not install a plugin
by symlinking or treating the repository root as a single plugin.

### Claude Code

Add the marketplace directly from GitHub; no clone is required:

```bash
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Confirm with `claude plugin list`. The plugin installs three user-facing skills, `research:project`,
`research:dashboard` and `research:retro`.

### Codex

Add the marketplace directly from GitHub; no clone is required:

```bash
codex plugin marketplace add nampham2/agents --ref main
codex plugin add research@agents
```

Confirm with `codex plugin list`.

### Pi

Install the repository as a Pi package:

```bash
pi install git:github.com/nampham2/agents
# Or from a clone:
pi install /path/to/agents
```

Confirm with `pi list`. Pi exposes the three skills as `/skill:project`,
`/skill:dashboard` and `/skill:retro`. Pi does not add the plugin `bin` directory to `PATH`;
each skill runs the launcher beside its own `SKILL.md`.

## Python

The research launchers need Python 3.11 or later. They need no virtual environment. Each launcher
uses the newest of `python3.14` down to `python3.11` on `PATH`, then `python3`. If none qualifies,
it runs through [`uv`](https://docs.astral.sh/uv/) (`uv run --python ">=3.11"`), which fetches a
Python once. Without either, it exits with a message that names both fixes. Stock macOS `python3`
is 3.9.6, below the floor: install a newer Python (`brew install python`) or `uv`. Releases
through 0.29.0 ran on Python 3.9.

## Development from a clone

```bash
git clone https://github.com/nampham2/agents.git
cd agents
uv sync --dev
```

For Claude Code, the helper validates and registers this clone as the local `agents` marketplace:

```bash
bin/install-plugin.sh research
```

For live Claude development without an installed cache:

```bash
claude --plugin-dir plugins/research
```

Claude permits only one source for a marketplace name. Switch a local `agents` marketplace back to
GitHub with:

```bash
claude plugin marketplace remove agents
claude plugin marketplace add nampham2/agents
claude plugin install research@agents
```

Removing a Claude marketplace also removes plugins installed from it.

## Releases

### 0.31.0 Goldfish review gates

The `project` skill adds an Elephant-Goldfish review, after the
[Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model) (Apache-2.0). Fresh,
read-only subagents (Goldfish) review one target; the coordinator triages their findings; the tool
records the round and refuses the next step without a current one.

- **Four gates.** `spec` before a requirements or alignment confirmation, `planning` before a
  planning or alignment confirmation, `design` before the first code task runs, and `diff` before
  a release record, before a merge record and before a no-remote worktree is closed. The design
  and diff gates apply only to a project with a code task.
- **Rounds.** `workflow <project-dir> goldfish` takes `operation` `open`, `close` or `waive`. A
  round is the managed document `reviews/goldfish_NN.md`; `project.json` gains no field. The tool
  computes the version a round covers (a content digest of the spec or plan, the design without its
  status table, or a commit) and the outcome it reaches. A fix to a Blocker or Major needs one
  re-audit; a re-audit that reports any Blocker or Major closes only as `accepted-residual`, with
  the user's words.
- **Waiver.** With no fresh subagent the review stops, and the gate passes only through a `waive`
  round that holds the user's words. A waiver replaces the round, not the checks on branch, clean
  tree, pushed upstream or merged head.
- **Diff checks.** A release needs a clean worktree on its recorded branch, a pushed upstream at
  `HEAD` when a pull request is recorded, and a round for that commit. A merge record takes
  `merged_head`, the pull request head that was merged: it must match a round, and the local branch
  head while that branch exists (a commit that builds on the branch head also passes). A no-remote
  worktree needs a round before its first closure and before every remove; if its branch is gone,
  its latest passed round stands. The tool never fetches: push first.
- **Live projects.** A gate applies to an event at or after `GOLDFISH_FROM`, which is
  `2026-10-27T12:13:54+01:00` in `workspace_lib.py`: the time the version bump began, a little
  before its commit. Earlier events count as passed and nothing re-checks them. Validation,
  `resume` and `readiness` warn, never error, about an open round, a stale spec or planning round,
  and a gate event after the instant that has no round, which is what an older launcher leaves
  behind. A stale design or diff round shows only at its gate. A closure of
  a pull-request worktree with no merge recorded only warns; only a no-remote worktree's closure
  is a gate.
- **Words.** The coordinator procedure is `references/goldfish-review.md` (300 words, the cap,
  test-enforced). Reviewer prompts are in `references/goldfish-personas.md`, read only by the
  reviewer subagents. With the pointer sentences, the `goldfish` row of `automation-records.md`
  (which carries the field shapes) and its name in the router, the instruction paths grow against
  0.30.4: alignment by 402 words, closure by 471, resume by 100, material change after resume by
  102, and the records branch of the default payload by 92 (the entry file by 1, to 899 of 900).
  The budgets and the 0.21.0 cap in `test_lifecycle_payloads.py` rose by exactly those amounts, by
  the user's decision. Three unrelated `SKILL.md` phrases were cut to make room: "including
  alignment" from the `commands.md` instruction, "directly" from "Apply routine corrections", and
  "through" from "Send a compact patch through stdin".

Limits, stated: the tool proves that its own path ran, not that the reviewers did, and a
hand-written round file passes a gate, as a hand-written confirmation marker does. The severity and
triage labels are the coordinator's: nothing can tell a Blocker relabelled as a Nit from an honest
Nit, and a rewrite that comes with only Nit fixes is accepted without a re-audit. The warnings for a
release, a merge or a code task started after the instant look for a passing round, not for the
version that shipped, because those records store no commit. A project from before the task stamps
that blocked a code task which had run, with no evidence, asks for a design round once when it
resumes. An older project that predates the delivery gates can still close a pull-request worktree
with no merge record; validation warns.

Compatibility: no field is added to `project.json`. A 0.30.x launcher reads a round document as
plain Markdown and enforces no Goldfish gate, so refresh the plugin before opening a project from
another host. Older projects open unchanged.

### 0.30.0 duplicate guard, optional alpha, stop reasons, planning and design documents

Six changes to the `project` skill, shipped together on one branch.

- **Duplicate guard.** `init` scores the new title against the live projects, with a higher bar
  when the working directory differs, and creates nothing on a likely match. It prints the
  candidates; `init --acknowledge-duplicates <ids>` proceeds once the user has answered, and the
  ids go into the specification history. `find-similar` runs the same score, and
  `list-projects --query` matches words, not substrings.
- **Alpha reset and occupied bases.** When the base moves, the alpha number restarts at 1. The
  tool refuses a base that main released (`origin/main` or a version tag), another live project
  holds, or another `origin/*` branch, local worktree or local branch carries, and it names each
  occupier. The check reads local refs, so run `git fetch --prune` first.
- **Optional alpha.** A project records an alpha line only for a test or experiment build. A
  repository project closes with a `release` record, whose base passes the occupancy check, or an
  exemption with a reason. An exemption and a release record exclude each other.
- **Stop reasons.** `task finish`, `task skip`, `task block` and `resume` return `continue` or one
  of six `stop_reason` values (`references/stopping.md`). A checkpoint with session `waiting`,
  `blocked` or `ready for handoff` must name one; `working` carries none.
- **Planning and design documents.** The agreement is `planning.md` (`planning` document id,
  `kind=planning` markers, `Agreed planning` link). The technical design is
  `artifacts/architecture.md`, written by the first task of a project that writes code and never
  gated. For a project created after the release instant, validation warns when a code project's
  design is missing, lacks a section its profiles require, or has no status row for a `DONE` code
  task. A project begun before the split keeps a root `architecture.md`, which the tools read as
  the planning document; the old `architecture` kind, token name, marker and checkpoint key still
  work, and a legacy project that names the design id is told to use `planning`.
  `references/architecture-review.md` is now `planning-review.md`; `design-document.md` is new.
- **Python 3.11.** The launchers need Python 3.11 or later and pick the newest on `PATH`, then
  `uv`. The `python-floor` CI job replaces the 3.9 job.

Compatibility: a 0.29.0 launcher cannot read a project that records `planning.md`, a
`stop_reason`, an `alpha` of `null` (a release-only line) or a reset alpha history. Refresh the
plugin before opening such a project from another host. Older projects open unchanged, and the
replay of validation over 119 workspace projects found no new error.

### 0.28.2 finalize stages the reflection's lessons

`workflow finalize` now appends the reflection's own lesson sections to the project's
`memory-staging.md` in the same commit that writes the reflection. Every level-two reflection
heading whose title names a lesson — one of `did not work`, `went wrong`, `got wrong`, `failure`,
`cost time`, `lesson`, `differently`, `wrong` — is staged whole and verbatim. The close returns the
titles it staged and the titles it skipped, and the triage-at-close warning is computed from the
appended content, so the next session decides promote, reflection or discard instead of leaving
those lessons in the reflection only.

- **One entry per heading.** `append_staged_section()` is the shared builder and `stage_lesson`
  now sits on it, so both paths refuse a title carrying a newline, a leading `#` or more than 120
  characters, and a body carrying a heading or nothing at all.
- **Skip, never raise.** A staged title with the same body is reported as `already staged`, a
  title with a different body as `title conflict`, and an illegal body is skipped with its reason.
  A reflection with no matched heading leaves the file byte-identical.
- **Token and replay.** The close requires the current `memory-staging` token, publishes it from
  `context --validate`, and writes the reflection and staging in one commit; replaying the same
  operation ID appends once.

### 0.28.1 refuse relative paths that resolve only in the project workspace

Every command-bearing `research-project` route — direct `record-evidence`, workflow check and
control batches, and the closure `--step` action — resolves relative path arguments against the
target working directory, and refuses an argument that fails to resolve there while resolving in
the project workspace. The refusal happens before any process starts, prints the cwd the command
would use plus the path it found on both sides, and records no evidence.

- **Both workspace roots.** A path under the project directory and a path under the workspace root
  each count as a workspace-only resolution, and one message lists every refused site.
- **Inline shell scripts.** Tokens inside `bash -c` and other inline scripts face the same rule at
  any position, including command substitution such as `$(research-validate .)`; an unbalanced
  quote falls back to whitespace splitting instead of skipping the script.
- **Workspace-aware tools.** `research-project` and `research-validate` named as a command refuse
  a relative argument that would not name the project or workspace, even when it also resolves in
  the target, and the message suggests the absolute project directory instead.
- **Paths that resolve in the target run.** Target-relative arguments, names present in both
  roots, and path-shaped prose that resolves nowhere are unchanged.

### 0.27.0 retro skill and the records it needs

`research:retro` reads the records of the projects closed since the previous retro, proposes changes
to the plugin itself by severity with evidence pointers, and records the user's accept, reject or
defer per candidate in a ledger under the workspace root. Three records make a project retro-ready.

- **Invocation log.** `research-project` and `research-validate` append one JSON line per
  project-scoped call to `<project>/invocations.jsonl`: command, action, exit, error class
  (`conflict`, `operation`, `refused`, `nonzero`, `crash`), message, duration, plugin version. The
  line is written after the command, a write failure is swallowed, and only a directory that holds
  `project.json` is written to.
- **Friction entries.** `research-project append <project> friction --kind <kind> --pointer <ref>`
  records what got in the agent's way, in seven kinds, into `<project>/friction.md`. `read <project>
  friction` returns them.
- **Retro section.** `finalize` requires a `## Retro` section of at least 120 characters in the
  reflection for a project created on or after the release instant, and warns for an older one.
  `readiness` and `research-validate --close` report the same finding under `retro`.
- **Commands.** `research-project retro batch <root> [--since <date>] [--project <id>]` returns the
  closed projects after the last run's cursor, each folded into one bounded row (layers, invocation
  failures, friction by kind, retro section, failed evidence, optional Claude Code transcript
  tallies), plus the rejected and deferred candidates of earlier runs. `research-project retro
  record <root> -` writes `retro/<id>.md` and `RETRO.md`; it is the ledger's only writer.
- **Instruction cost.** The project skill gained the friction command and the retro section rule at
  net zero words; the budgets in `test_lifecycle_payloads.py` did not move.

### 0.25.0 local dashboard

A read-only web page over the research workspace. `research-dashboard` starts it, and the
`research:dashboard` skill starts it in the background for an agent. Nothing adds a field to
`project.json`.

- **The page.** One list of every project, grouped by the day it last moved, newest first. A row
  shows the status on a five-stop rail, the repository, the task progress, and one word when the
  project waits on you. A row opens in place into the tasks, the decisions, the handoff, the
  worktrees and the merge request. Filters: status, moved since (updated or created), repository,
  search, and needs you. The page refreshes when a project file changes.
- **Claude sessions.** A project lists the session it recorded, then up to three sessions whose
  transcripts show work on it, newest first. Each has a button that copies
  `cd <directory> && claude --resume <id>`. The checkpoint in `handoff.md` now records
  `CLAUDE_CODE_SESSION_ID` and the directory, so a project touched from this release names its exact
  session. Older projects use the transcript search under `~/.claude`.
- **Command.** `research-dashboard [ROOT] [--port 8765] [--no-browser] [--claude-dir ~/.claude]`.
  The root defaults to `$RESEARCH_WORKSPACE`, else the one established root. No root or several
  roots is exit code 2. The server binds `127.0.0.1`, answers only requests that name that address,
  offers no write, and stops on Ctrl-C.
- **Constraints kept.** Standard library only, Python 3.9, no build step. The `python39-compat` CI
  job starts the server on 3.9 and requests the project list.

### 0.23.0 writing rules and advisory style findings

Project records now follow writing rules that ASD-STE100 inspired, and the validators give advice
when a living record does not. Nothing adds a field to `project.json`, so older launchers read
everything this release writes and simply give no advice.

- **Writing rules.** `references/writing-rules.md` holds 45 rules and a ten-term glossary. Thirteen
  rules come from the public list of STE rules and 32 are the project's own; each is marked. The
  file adds 774 words to every scenario that writes records. `references/writing-rules-extra.md`
  holds the preferred-word table and the STE rules the project does not apply, and is read only on
  demand. The project has no official copy of ASD-STE100, claims no compliance, and says so.
- **Advice, never a block.** For projects created on or after 2026-10-05T10:40:00Z, `research-validate`
  and `context --validate` report up to five findings per document and count the rest: sentences over
  25 words (20 for the task `verification` field and for numbered items in `handoff.md`), paragraphs
  over six sentences, preferred words, contractions, and `will`, `should` or `could`. The check reads
  the current specification, `architecture.md`, `handoff.md` and the task fields, in every status.
  It checks form only. About 40 of the 45 rules have no code check, because they need a parser.
- **Word budgets.** Each scenario budget rose by the 774 words. The alignment guard changed from
  10 percent below the 0.21.0 size to below it: alignment (repository) is 7,746 words against 7,813,
  a margin of 67 words.

### 0.22.0 one rule per transition, agreement tied to content, compact alignment

Fixes for eight findings of a review of 0.21.0, where the same transition was allowed or refused
depending on which command asked. Nothing adds a field to `project.json`, so a 0.21.0 launcher still
reads everything this release writes; it simply does not enforce the new rules. New checks judge only
the transition being committed: a task already `DONE` and a project already closed are never
re-judged.

- **Agreement is tied to content.** `workflow confirm` records a digest of the text the user agreed
  to beside its confirmation. Rewriting the specification or `architecture.md` afterwards makes the
  confirmation stale: the gate refuses `ALIGNING` to `PLANNING` and names the changed document, and
  `edit` and `round` return `stale_confirmations`. Decision history, the architecture link, the
  status word, the confirmation block and blank lines do not count as changes.
- **Finishing a task is one rule.** An `update` setting a task `DONE` is refused for a failed,
  foreign or ambiguous evidence record, as `task finish` always was. A reference the tool did not
  record cannot be judged: a warning for projects created before the cutoff, an error after it, and
  an observation-only finish must go through `task finish --observation`.
- **Closing a project is one rule.** `close` and an `update` to `DONE` refuse a recorded worktree
  with no keep or remove decision, as `finalize` did; `workflow readiness` and
  `research-validate --close` report the same finding once.
- **Worktree decisions can change.** `keep`, `accept_dirty` and `remove` may be recorded again on a
  kept worktree; the earlier decision is quoted into the decision history first, so the call needs
  the `spec` token. `accept_dirty` covers the paths dirty at that moment, and a new path is a closure
  finding. A removed worktree stays final.
- **Malformed worktree records** are findings, not a crash in validation.
- **Compact alignment.** `workflow confirm` accepts `kind: alignment`, one reply recorded against both
  document tokens, for a clear low-effort proposal. The full alignment reading path is 10.8 percent
  shorter (7,813 to 6,966 words) after removing duplicated procedure, and two pieces of guidance now
  load only when needed: `handoff-verifiers.md` and `memory-promotion.md`.
- **Tests.** The instruction budgets count every required read and a routing-drift test fails when a
  loaded reference links one that is neither counted nor classified; a complete current-project
  lifecycle runs through both launchers as subprocesses.

Compatibility: a project confirmed by 0.21.0 that has not yet left `ALIGNING` has no content digest;
0.22.0 refuses it with a message to confirm again. A project that already left `ALIGNING` is not
affected.

### 0.21.0 alignment gate and task stamps for new projects

Two changes that apply only to projects created on or after the release instant recorded in
`GATES_ENFORCED_FROM` (`workspace_lib.py`); every project that existed before it keeps the warnings
it always had, and no older launcher is locked out by the gate itself.

- **Alignment gate.** A new project may not leave `ALIGNING` (or `BLOCKED`) for `PLANNING` or
  `EXECUTING` until its seven specification sections are filled, `architecture.md` is agreed, and
  `workflow confirm` recorded both a requirements and an architecture confirmation. The refusal
  names each missing fact and the command that records it. Only that transition is gated; a reopen
  and a draft design revision mid-execution go through. It proves the tool's confirmation path ran,
  not that the user agreed.
- **Task stamps.** A new project's tasks carry `started_at` (first start; a restart after `BLOCKED`
  keeps it) and `finished_at`; `--backfill` sets `finished_at` only. The task graph prefers them to
  evidence stamps.

Compatibility: a launcher older than 0.21.0 cannot read a project whose tasks carry the stamps; it
reports the unexpected field and names a newer plugin as the likely cause. Update the plugin and
restart before resuming such a project.

### 0.20.0 validation agrees, observations, discoverability

- Standalone validation and the commit path now agree about finished work whose files moved: a
  finished task's missing required output or evidence file, and a finished project's missing working
  directory, are warnings on every path. 35 of 87 historical projects failed `research-validate` only
  for this; none do now, and no project that passed fails. A task becoming `DONE` still errors.
- A reopened project can record a new target worktree after the earlier one was removed, at a new
  path; only the latest target constrains the target root.
- `research-project record-observation` records a check with no command (an MCP read, a query) as an
  agent-attested entry, finished on only with `task finish --observation`; `--evidence` refuses it.
- `workflow <action> --schema` prints an action's allowed and required keys from the same table that
  validates it; input errors name the offending key and the action; `--help` names `research-project`.
- `list-projects` shows `created` and `updated` and takes `--older-than-days`.

Compatibility: nothing in this release adds a field an older launcher rejects. An older launcher sees
an observation entry as not selectable and refuses to finish on it.

### 0.19.0 recorder, handoff and launcher guards

Built from what 85 past projects actually tripped over:

- `record-evidence` refuses a relative path given to `research-validate` or `research-project`,
  exports `RESEARCH_PROJECT_DIR`, adds `--cwd` and `--dry-run`, and warns (without blocking) on
  evidence for a task that is not `RUNNING`, a heredoc or an unguarded pipe inside `bash -c`.
- `task finish --backfill --note` records work done before `task start` as two guarded commits.
- Handoffs gain a first-class `do_not` field, a stale-handoff banner, and a closure hint;
  `finalize` refuses a reflection that is not a post-mortem; a kept worktree can be recorded as
  removed.
- Every command warns on stderr when a cached, out-of-date plugin copy is running.
- `research-project stage` writes a triage-ready staged lesson; `promote-memory` is idempotent.

Compatibility: a handoff written with `do_not` cannot be read by an older launcher. Update the
plugin, restart Claude Code, and then resume projects.

### 0.18.1 effort scale

Effort in architecture proposals is rated low, medium or high per workstream and overall, anchored
on scope signals, and never estimated in hours or days. External waits are named as dependencies.

### 0.18.0 git worktree discipline

Every repository a project writes to gets a user-confirmed, recorded git worktree before the first
write (`workflow worktree`, `record` and `close`), and closure needs the user's commit and
keep or remove decision for each one. `project.json` gains an optional `worktrees` list; older
launchers cannot read a project that records one.

### 0.17.1 checkable handoffs

Handoffs follow the provenance, verifier-status and preservation rules in `handoff-writing.md`:
a claim the successor will rely on carries its source and observation time, and one that was not
checked is marked `UNVERIFIED` with the re-check.

### 0.17.0 execution change

Projects now use normal task execution and scoped native subagents. The automatic executor and
`run-auto`, `run-once`, `run-parallel`, and `enable-execution` commands are removed. New projects
create no `execution/` store or empty `tasks/`, `artifacts/`, or `reviews/` directories.
Existing idle v3/v4 projects remain supported without migration; historical files are untouched.
Resolve active legacy executor ownership with the previous compatible installation before upgrading.
This version preserves the ownership guard but cannot perform legacy executor recovery.

### Versioning and updates

`pyproject.toml` owns the release version. `uv.lock` and the Claude Code, Codex and Pi plugin
manifests must use exactly the same SemVer; CI enforces this contract.

Claude Code caches installed plugins by version. After publishing a version bump:

```bash
claude plugin marketplace update agents
claude plugin update research@agents
```

Restart Claude Code to apply the update.

## Checks

```bash
uv run pytest -q
uv run ruff check .
uv run ty check .
uv lock --check
```
