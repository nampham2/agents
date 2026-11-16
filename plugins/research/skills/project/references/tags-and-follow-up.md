# Tags and follow-up projects

Read this when you create a project, change its tags or close it. Every new project has 1 to 5 tags.
The dashboard shows them and filters by them.

## What a tag is

A tag names the kind of work, not its status: `dashboard`, `bigquery`, `research-plugin`. It is
lowercase words joined by single hyphens, at most 24 characters. The tools clean the text first:
they lowercase it and turn spaces and underscores into hyphens, so `My Tag` becomes `my-tag`. They
refuse a tag that is still not in that form, a duplicate and a sixth tag. They never truncate.

## Ask for tags when you create a project

1. Run `start` without `--create`. If it lists candidates, resume one or ask the user which; ask for
   no tags, because a resumed project keeps its own.
2. If it finds nothing, get suggestions:

   ```sh
   research-project suggest-tags [root] --title "<objective>" --working-directory <target>
   ```

   Each suggestion has a `tag`, a `source` and up to 5 `projects`. The sources are `repository`
   (used by projects on the same repository, most used first), `title-similar` (used by projects
   with a similar title) and `title-word` (a significant word of the title). `--limit` is 1 to 50,
   default 10. Read a tag the user picked from a suggestion as the user's own choice.
3. Ask the user for 1 to 5 tags. Offer the suggestions as a pick-list and accept other words. Do
   not choose the tags yourself.
4. Create the project with the answer:

   ```sh
   research-project start --create --workspace-root <root> --title "<objective>" \
     --working-directory <target> --tag <tag> --tag <tag>
   ```

   A title is one line and cannot contain `<!--`. Creation strips the title first, so a trailing
   newline is dropped. `update` refuses a title with any line break, trailing ones included, and
   `research-validate` reports one in a v3 or v4 `project.json`. A bad title that gets into
   `project.json` by hand makes every state command refuse the project, naming the title, and
   blocks migration of a v1 or v2 project. Fix it in `project.json` (or the v1 plan heading) by
   hand. `init <root> ... --tag <tag>` takes the same flag. With 0 tags, more than 5, a duplicate
   or a bad tag the command prints `init created nothing` and writes nothing.

## Change tags later

```sh
research-project tag <project-dir> add|remove <tag>... --expected-revision <revision> [--json]
```

The command goes through the normal commit path, so it bumps `revision` and `updated`. Every refusal
starts with `tag refused:` and writes nothing. The causes are:

- a schema v3 project (run `research-project migrate <project-dir>` first);
- a recorded `coordinator_run` or non-empty `attempts`: legacy executor ownership, see
  `references/legacy-executor.md`;
- a stale `--expected-revision`;
- a tag already present, a tag not present, a sixth tag, or removing the last tag;
- a stored `tags` field that is not a valid list (fix `project.json` by hand).

Projects from before tags have none. They stay valid and show as untagged; `tag add` gives them
their first tag. Nothing adds tags to them automatically. `update` and `reconcile` refuse a `tags`
patch: `tag` is the only way to change them.

## Reading tags

`research-project list-projects` rows carry `tags` (an empty list for a project without any) and,
when the project names one, `predecessor`. `suggest-tags` reads those rows.

## Follow-up projects

A follow-up is a new project that continues the open work of a finished one. You suggest it. The
user agrees or declines. You never start work in it: it stays in `ALIGNING` until the user runs it.

### Before you close

Write a `## Follow-up` section in the reflection: one bullet for each piece of work, risk or open
question that the project leaves behind, and why it matters. Do not list work that is done. A bullet
may wrap onto the next line, and an indented line or a nested bullet joins the bullet above. A
section with text and no bullet gives a warning, and a section with no bullets gives no items. The
section is never stored as a lesson, whatever its title says. A fenced block that is indented four
or more spaces inside a numbered item is not tracked as a fence (the same limit as headings). Name
it `Follow-up` or `Follow-ups`: a title that only starts that way, such as `Follow-up work` or
`Follow up`, is not read, and `finalize` warns about it. It also warns when a fenced block in the
section is never closed, because that hides the bullets below it.

### After `finalize`

`finalize` returns `follow_up`, a list of items, and `follow_up_warnings`. Show the warnings to the
user. Each item has a `kind`, a `text` and a `pointer`:

- `reflection`: a bullet of the `## Follow-up` section. The pointer is `reflection.md`.
- `residual`: a Blocker or Major finding of a Goldfish round that closed `accepted-residual`, and
  that was not fixed. The pointer is `reviews/goldfish_NN.md#<finding id>`. Per series, only the
  latest such round counts, even after a later round passed, so a list may carry an item that a
  later round already fixed. Say so when you suggest it. A round file that cannot be read, or whose
  first line starts like a round marker (`<!-- research-goldfish`) but does not parse, gives one
  `residual` item that says so: open the file. So do lines of a findings section that are not
  finding lines. A file with no marker on its first line is not a round and is left out.

If the list is empty, tell the user that no follow-up is needed and ask nothing. If it is not empty:

1. Show the items and suggest a follow-up project with a proposed title. Show the warnings too.
2. Wait for the user's answer. A decline stores nothing and ends the matter; the user can ask for
   the follow-up later. Never create it without an answer that agrees.
3. If the source has no tags (most projects from before tags), ask for 1 to 5 as for any new
   project: `suggest-tags`, then the user's choice. Otherwise the source's tags are the default, and
   `--tag` replaces them.
4. Create it, with the user's own words as the agreement:

   ```sh
   research-project follow-up <source-project-dir> --title "<title>" --agreement "<the user's words>" \
     [--tag <tag>]... [--working-directory <dir>] [--json]
   ```

5. Report the new project's path and `Use the project skill to resume <path>.` Then stop. Do not
   resume it, plan it or write in it.

The command only reads the source. It refuses, writes nothing and prints a message that starts with
`follow-up refused:` when:

- the source is not `DONE`, its `project.json` is unreadable, or its `project` field does not match
  its directory name;
- the title has more than one line or contains `<!--` (a new project's title is one heading line,
  and a marker in it could forge a record marker);
- the agreement is blank, has a line that starts with `#`, or contains `<!--` (ask the user to
  rephrase: these could add a heading or forge a record marker);
- the working directory is gone or missing (pass `--working-directory`);
- the source has no tags and no `--tag` is given, or the tags are bad;
- a follow-up of this source already exists (one per source; the message names it).

### What it creates

A project beside the source, in `ALIGNING`, with no tasks and no worktree. `predecessor` names the
source. `tags` are the source's or the given ones. `working_directory` is the given directory, or
the source's, mapped to its repository when it is a worktree path. `spec.md` records the user's
agreement word for word in a fenced block in the decision history. `briefing.md` starts with the
line `Follow-up of: <source-id>` and lists each carried item under `Open questions for grill`, which
the requirements interview reads first. Nothing in the source changes.

The check for an existing follow-up reads `predecessor` and that first line. It runs under a lock
for this source that lives in the workspace root (`.follow-up-<source-id>.lock`), so two commands
for one source cannot both pass and nothing is written into the finished project. A crashed run can
leave that directory: it makes later follow-ups of that source wait for `--lock-timeout` seconds and
then refuse; clear it with `research-project unlock <that directory>`, which removes it only when
its owner is gone. The check is best effort otherwise: it cannot see a project whose `project.json`
is unreadable (the command warns with a count), a briefing or a reflection it cannot read is a
warning, and a briefing someone edited no longer carries the marker. A path that holds no
`project.json` is refused before any lock is taken.

Two failures can come after the project exists. A failure to release the lock says `the follow-up
project <path> was created, but then: ...` and repeats the warnings. A failed index rebuild keeps
its own message, which names the project and says to run `rebuild-index`. In both cases the project
is there: do not run the command again.

## In the dashboard

Tags show on cards and rows and in the project drawer. The tag bar filters: each chosen tag narrows
the list (a project must carry all of them), and each count says how many projects would remain if
that tag were added. `untagged` lists the projects with none.
