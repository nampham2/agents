---
name: dashboard
description: >
  Open the local research dashboard: a web page that lists the projects in the research workspace
  with status, progress and next action, filters them, and copies the command that resumes a
  project's Claude session. Use when the user asks to see, browse or filter their research
  projects.
---

# Dashboard

Invoke with `/research:dashboard [workspace root]`; in Pi, `/skill:dashboard`.

The dashboard only reads. It never writes to the workspace, `~/.claude` or `~/.pi`.

## Start it

1. Use `../project/scripts/research-dashboard` beside this `SKILL.md`. Use `research-dashboard` from
   `PATH` only for a Claude plugin. Keep that launcher.
2. Use the workspace root the user gave. Without one, pass no root: the launcher reads
   `$RESEARCH_WORKSPACE`, else it finds the one established root.
3. Start it in the background: `research-dashboard [ROOT] --no-browser --port 0`. The first
   option keeps it from opening a window. The second takes any free port.
4. Read the first output line, `Dashboard: http://127.0.0.1:<port>`. Give the user that URL.
5. If the launcher exits with code 2, show its message and stop. Never retry with a guessed root.

The server answers only on `127.0.0.1`, and only for a request that names that address.

## Stop it

Stop the background process when the user is done. In a terminal, Ctrl-C stops it and exits 0.
By hand the launcher listens on port 8765 and opens the browser: `research-dashboard`.

## What the page offers

- A status bar under the title: one chip per status with its count. A chip filters the page, and
  its count follows the other filters. Only the sections that hold a match stay on screen, so
  Done shows Finished alone and Blocked shows the board alone.
- A "Waiting on you" board first: every project that needs you, grouped by reason (blocked, needs
  your approval, review is waiting, waiting for your reply, or a `project.json` that cannot be
  read or has no status), five rows at first and "Show all" for the rest.
- Projects in flight as cards grouped by status, each with a five-stop route (hover a stop for its
  name and state), task progress and a chip for each plan or review gate that has something to say.
- Finished and cancelled projects behind a toggle. A done or cancelled filter, or a search that
  matches one, opens them.
- Filters: search, repository, a date since (by last update or by creation) and the status chips.
  A line under them says how many projects remain and offers a button per filter to remove it. A
  combination with no match says so and offers to clear it.
- Every project has a "Resume prompt" button that copies `Use the project skill to resume <path>.`
  Click a project id to copy it. A session adds a "Resume command" button in the drawer, which
  copies `cd <directory> && claude --resume <id>`.
- A drawer per project: phase and gates, next step, open questions, blockers and waits, tasks with
  evidence and receipts, counts, decisions, worktrees and merge request, and the sessions.
- Light and dark themes, a theme button, and keyboard use: Enter opens a project, Escape closes it.
- The page refreshes itself when a project file changes.
