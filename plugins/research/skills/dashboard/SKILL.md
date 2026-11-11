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

- A "Waiting on you" board first: every project that needs you, with the reason (blocked, needs
  your approval, review is waiting, waiting for your reply, or an unreadable `project.json`). It
  shows five rows and a "Show all" control; each row has a button that copies the resume command.
- Projects in flight as cards grouped by status, each with a five-stop route, task progress and
  chips for the plan, Goldfish review and delivery review. A strip under the title counts them.
- Finished and cancelled projects behind a toggle. A done or cancelled status filter, or a search
  that matches one, opens them.
- Filters: search, waiting on you only, status, repository and moved since (updated or created).
- A drawer per project: phase and gates, next step, open questions, blockers and waits, tasks with
  evidence and receipts, counts, decisions, worktrees and merge request.
- Claude sessions: the session the project recorded, then the sessions that worked on it, each
  with a copy button for `cd <directory> && claude --resume <id>`. With no session the button
  copies `Use the project skill to resume <path>.`.
- Light and dark themes, a theme button, and keyboard use: Enter opens a project, Escape closes it.
- The page refreshes itself when a project file changes.
