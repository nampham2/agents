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

- One list of every project, newest movement first, grouped by day, with a status rail.
- Filters: status, moved since (updated or created), repository, search, needs you.
- A detail panel per project: tasks, decisions, handoff, worktrees and merge request.
- Claude sessions: the session the project recorded, then the sessions that worked on it, each
  with a copy button for `cd <directory> && claude --resume <id>`.
- The page refreshes itself when a project file changes.
