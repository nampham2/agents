# Cross-project memory architecture

Read this reference before changing how the `project` skill records or retrieves what earlier
projects learned. It specifies the layout, the topic-file contract, index generation, retrieval,
the write path, validation severities, and migration. It also states what is deliberately excluded
and why, because most of the excluded machinery is what a reader coming from other memory systems
will expect to find here.

## The failure this replaces

Before this design, cross-project memory was one flat file, `reflection.md`, in the workspace root.
Its guardrail counted entries: `REFLECTION_MAX_ENTRIES = 20` at `scripts/workspace_lib.py:1225`,
compared against `reflection_entries` at `scripts/workspace_lib.py:1249` and reported by
`reflection_warnings` at `scripts/workspace_lib.py:1333`.

That guardrail measured the wrong unit. The live file reached **61,027 bytes across 752 lines** —
roughly 15,000 tokens, a fifth of a small context window — while counting as **15 entries** against
a cap of 20, so validation passed with no warning at all. The two entry shapes it counts are two
bulleted entries and thirteen dated `###` clusters.

The divergence was not accidental. The file's own preamble instructs that clusters are "merged in
place as they accumulate", and `SKILL.md:463` says to "merge or retire" rather than append. Merging
is the right editorial instinct and it *lowers the entry count while raising the byte count*: the
prescribed discipline moves the file away from the only thing being measured. `SKILL.md:468`
already states the real invariant — "A reflection file too long to be read in full stops being
memory and becomes an archive" — but nothing enforced it.

Two further facts shaped the design:

- `SKILL.md:228`, step 1.7, is the **only** step that reads cross-project memory, and it reads the
  whole flat file. There was no way to read part of it.
- About 219 KB of per-project post-mortems exist — 22 files across 26 project directories
  (`<project>/reflection.md`, `SKILL.md:97`). Nothing reads them and nothing indexes them. Across
  the eleven most recent, their `#` titles carry four different prefixes, two are bare (`#
  Reflection`) and name no project at all, and the rest diverge in wording from the canonical
  `title` in `project.json`. None carry frontmatter, so they cannot be self-indexed.

The design below therefore separates **cheap discovery pointers** from **detail loaded on demand**,
and enforces a budget on the first in the unit that actually costs the reader: bytes and lines.

## Layers

Three layers, ordered by how often each is loaded. Each layer is optional: a workspace root missing
every one of these files stays valid, and so do its projects.

### Layer 1 — `MEMORY.md`, generated, consulted on demand

`<workspace-root>/MEMORY.md` is a generated index of pointers. It is read in full at discovery,
when relevant to the work, and it is the only memory file with a hard size budget:

- **120 lines** and **12 KB**, whichever binds first. Exceeding either is an **error**.

It is generated, never hand-edited, in exactly the way `INDEX.md` is generated
(`references/workspace-schema.md:15`). It holds one line per topic file — the topic's name, its
one-line description, its scope, and its path — grouped by `kind`, and then **one** line pointing at
`POSTMORTEMS.md` with the number of post-mortems it indexes.

The budget is what makes the layer honest, and what the budget is applied to is what makes it
payable. Everything in this file is a topic pointer, and a topic pointer is the one thing a person
can merge or retire — so when the budget binds, the remedy the error names is a remedy the reader
actually holds.

An earlier version of this design put the post-mortem pointers here too, one line per project. That
was wrong in a way worth recording, because it was only visible in measurement. Those lines are
generated from canonical state, one per project directory, and **nothing retires them**: the term
was monotonic. Measured on a real root at the moment it first went over budget, of 12,393 bytes the
25 topic pointers held 5,480 (mean 219 B) and the 49 post-mortem pointers held 6,577 (mean 134 B).
So the only prescribed remedy drained a finite, purposeful pool at roughly 200 bytes a merge against
a growth term of ~134 bytes per project — about one and a half future projects bought per editorial
act, and the instruction ran out before the workspace did. The same numbers show the two bounds were
never calibrated to each other either: at that mean line width the byte cap binds near **74** lines,
so the 120-line bound could not be reached and the "40 topics plus 40 post-mortems" capacity this
document once claimed was never available.

Post-mortems therefore render into their own generated file, described with Layer 3 below, and the
budgeted file links to it in one line. The discovery index grows only when a person adds a
topic, and shrinks when a person merges one.

### Layer 2 — `memory/<slug>.md`, one topic per file, read by pointer

`<workspace-root>/memory/<slug>.md` holds the durable lessons themselves, one topic per file. A
topic file is read only when a pointer in `MEMORY.md` — or a `search-memory` hit — says it is
relevant. Topic files have **no individual size limit**: a lesson that needs 400 lines of detail
may have them, because nobody pays for those lines until the pointer says to.

That is the whole trade this architecture makes. The old file was expensive because detail and
index were the same bytes. Separating them means depth is free and breadth is budgeted.

Each topic file begins with frontmatter of exactly six fields:

```markdown
---
name: workspace-root-resolution
description: Never infer the workspace root; resolve it in a fixed order and ask when unsure.
kind: preference
scope: any research:project session
sources: 2026-08-29-001, 2026-08-30-001
updated: 2026-09-09
---

The body: the lesson, what happened that taught it, and what to do differently. Any length.
```

| Field | Meaning | Rule |
| --- | --- | --- |
| `name` | Slug identifying the topic | Non-empty; must equal the filename without `.md` |
| `description` | One line, the whole reason to open the file | Non-empty; single line |
| `kind` | Which group the pointer appears under | One of `preference`, `environment`, `method` |
| `scope` | When this lesson applies | Non-empty; free text |
| `sources` | Project ids that produced the lesson | Comma-separated `YYYY-MM-DD-NNN` ids; may be empty |
| `updated` | Date the body last changed | `YYYY-MM-DD` |

The three `kind` values are the three sections the flat file had already grown for itself (`##
Confirmed user preferences`, `## Environment and tooling`, `## Method`). They are a closed set so a
typo is refused rather than silently creating a fourth group.

`description` and `scope` carry the retrieval weight: they are what a reader sees in `MEMORY.md`,
and they are the fields matched first by search. A vague description makes a topic unreachable
however good its body is.

**Never rewrite a topic file from scratch.** Amend the body, add to `sources`, bump `updated`. A
rewrite loses the incident that made the lesson credible, and provenance is the only thing that
distinguishes a lesson from an opinion.

### Layer 3 — per-project post-mortems, read rarely, indexed not summarized

`<project>/reflection.md` stays exactly as it is: the post-mortem of one project, written at close
(`SKILL.md:443`), required non-empty for closure (`references/workspace-schema.md:368`). Its format
does not change.

What changes is that it becomes reachable. `<workspace-root>/POSTMORTEMS.md` lists it by canonical
project title, and `search-memory` searches its body. It is the deep archive: the place a specific
past project's reasoning can be recovered, without any of it being loaded by default.

`POSTMORTEMS.md` is generated exactly as `MEMORY.md` is — in full, under `.index.lock`, written
whenever `MEMORY.md` is written, so the pointer in `MEMORY.md` never names a file that is not there.
It carries **no size budget**, and that is the point: it is read on demand, by a reader who has
already decided a named project is worth opening, so its growth costs nothing at discovery. One line
per project directory holding a readable `reflection.md`.

Those lines are built from `project.json` `title` and `status` — the same canonical fields
`render_index` reads — and never from headings inside the post-mortem itself. Canonical titles cannot
drift from canonical state; document headings demonstrably have.

A lesson that generalizes beyond its project is promoted out of a post-mortem into a Layer 2 topic
file. A lesson that does not generalize stays in the post-mortem, which is now indexed rather than
lost.

## Retrieval

Three routes, in increasing cost:

1. **Always** — read `MEMORY.md` at discovery. Bounded by the budget, so this cost is fixed and
   known.
2. **By pointer** — open the topic files whose description and scope match the work at hand.
   Usually zero to three files.
3. **On demand** — `research-project search-memory <query>` when the pointers are not enough.
   The root comes from `--workspace-root` or `$RESEARCH_WORKSPACE`, and is an option rather than
   a leading positional because a query read as a root would report no hits, which is
   indistinguishable from a topic that does not exist.

`search-memory` searches Layer 2 frontmatter first and Layer 3 post-mortem bodies second, and
prints **paths with the matching lines**, never file contents. Printing paths keeps the cost of a
wide search proportional to the number of hits rather than the size of what was hit, and leaves the
decision of what to actually load with the reader. Frontmatter comes first because a frontmatter
match means the topic is *about* the query, while a body match may only mention it.

Search is lexical — substring and case-insensitive over the fields and files described above. It is
not ranked, not stemmed, and not semantic. See the exclusions.

## The write path: stage during, drain at close

Recording a lesson mid-project is a distraction from the project, and recording nothing until close
loses what was learned on day one. So lessons are staged as they happen and promoted once, at
close:

1. **During the work**, append the observation to `<project>/memory-staging.md`. Free-form,
   append-only, unbudgeted, unvalidated apart from the one warning below. Staging is cheap on
   purpose.
2. **At close**, drain it. For each staged observation, decide: promote it into a new or existing
   topic file with `research-project promote-memory <slug> --body … --source <project-id>`, fold it
   into this project's post-mortem, or discard it. Then empty the staging file. `promote-memory`
   amends rather than replaces — merging `sources`, bumping `updated`, and appending the new text
   below what is already there — because a rewrite loses the incident that made the lesson
   credible.
3. **Regenerate** `MEMORY.md` — which any `research-project commit` already does, because index
   regeneration is on the commit path.

A non-empty `memory-staging.md` at close is a **warning**, not an error: unfinished triage is a
fact worth surfacing, but it is not a reason to refuse to close a project whose actual deliverables
are done.

Promote a staged observation only when it is durable, sourced, and would change what a future
session does. An observation that fails any of those three belongs in the post-mortem or in the
bin.

Staging lives **inside the project directory**, not beside the topic files, and that placement is
the entire concurrency story of the write path. The next section says why.

## Generation, and why it cannot block a commit

`research-project rebuild-index <workspace-root>` regenerates `MEMORY.md` alongside `INDEX.md`,
under the same workspace index lock (`rebuild_index`, `scripts/workspace_lib.py:1727`). Generation
is idempotent: running it twice on an unchanged root produces byte-identical output.

That path is reached after every commit, through `_rebuild_index_after_commit`
(`scripts/workspace_lib.py:1465`). This coupling creates one hazard that the design must answer
explicitly:

> **A malformed topic file must degrade to a warning and must never block a commit.**

Memory is advisory, not canonical state. `project.json` is the only authoritative source
(`references/workspace-schema.md:8`). If a half-written topic file could fail index generation, it
could fail the commit that records a completed task — a note nobody has finished writing would be
able to block the record of work that is finished. So generation skips what it cannot parse,
records a finding, and produces a valid `MEMORY.md` from the rest.

Validation is where malformed frontmatter becomes an error. Generation is where it becomes a
warning. The two live in different places because they answer different questions: *is this
workspace's memory in good order* versus *may this commit land*.

## Concurrency

Several projects share one workspace root and more than one can be active at a time — that is why
`.index.lock` is scoped to the root rather than to a project. So every shared memory path needs a
stated writer discipline. There are two locks in the library and no append primitive: every append
here is a read-modify-write under a lock, which is exactly how `record_evidence` appends to
`evidence.md` at `scripts/workspace_lib.py:1717`.

| Path | Shared | Lock | Why that suffices |
| --- | --- | --- | --- |
| `MEMORY.md` | yes | `.index.lock` | Read and write both happen inside it |
| `POSTMORTEMS.md` | yes | `.index.lock` | Generated in full beside `MEMORY.md` |
| `memory/<slug>.md` | yes | `.memory.lock` | Read-modify-write is serialized |
| `<project>/memory-staging.md` | no | `.project.lock` | Only one project can write it |

**The generated files need no discipline of their own.** `rebuild_index` calls `render_index` *inside*
`.index.lock` (`scripts/workspace_lib.py:1727`) and replaces the file atomically, so there is
neither a torn read nor a torn write. Because generation reads the whole root rather than applying
a delta, the loser of a race regenerates from a filesystem that already holds the winner's work:
the outcome is correct whichever order two concurrent rebuilds land in. This is the guarantee
`INDEX.md` has had all along.

**Promotion into `memory/<slug>.md` takes a new `.memory.lock`** at the workspace root, for the
duration of the read-modify-write. Two projects closing at the same moment and amending the same
topic would otherwise be a silent lost update, the second write carrying none of the first's new
`sources` line. A separate lock rather than reusing `.index.lock`, because the commit path already
takes that one to regenerate the index, so promote-then-regenerate would nest it. `.memory.lock` is
therefore never held across a call that rebuilds the index.

**Staging is per project, which removes the race instead of locking it.** Because
`<project>/memory-staging.md` sits inside the project directory, the existing `.project.lock`
covers both the appends and the drain, and no other project can write the file at all.

An earlier draft put staging at `<workspace-root>/memory/STAGING.md`, shared. That was wrong, and
wrong in an instructive way, so it is recorded here rather than quietly dropped. The append raced,
because `.project.lock` is scoped to a project and gives a workspace-root file no protection
whatever — two projects would each take their own lock and write the same path. Worse, the drain
raced *destructively*: a note appended by one project between another project reading the file and
emptying it was lost, and lost **invisibly**, because an empty file is the intended outcome of a
drain. The "non-empty at close" warning would then have reported clean, so the guardrail meant to
catch unfinished triage would instead have confirmed the data loss.

The error was copying the shape of `evidence.md` — read, concatenate, `atomic_write_text` — without
noticing that moving the file up to the workspace root took it outside the lock that makes that
shape safe. The rule this design now follows: **a mutable memory file is either per project and
covered by `.project.lock`, or generated in full under a lock, or amended under `.memory.lock`.**
Nothing shared is appended to.

One consequence is worth stating. Because generation happens per commit and validation compares
against regeneration, a project that adds a topic file while another project's regeneration is
already in flight can leave `MEMORY.md` momentarily stale. That is an error only under
`--check-index`, and the fix is to rebuild — precisely the existing behaviour for `INDEX.md`
(`references/workspace-schema.md:15`).

## Validation

Errors — a project cannot validate, and cannot close, while one stands:

- **`MEMORY.md` exceeds 120 lines or 12 KB.** This is the one budget the whole design rests on, and
  the measured failure above is what happens when it is advisory. The bound is measured in **bytes**,
  as `len(content.encode("utf-8"))`: a pointer line holds an em dash at three bytes, so a character
  count reads a file over its cap as comfortably under it. `POSTMORTEMS.md` has no budget and cannot
  raise this one.
- **A topic file has missing or malformed frontmatter.** An unparseable pointer is an unreachable
  topic, so the body might as well not exist.
- **`MEMORY.md` or `POSTMORTEMS.md` disagrees with regeneration.** An error only under
  `--check-index`, exactly as `INDEX.md` is treated (`references/workspace-schema.md:15`). Both are
  checked, for the same reason: a stale one is a reader following a pointer to something no longer
  true.

Warnings — reported, never blocking:

- **`sources` cites a project id absent from the root.** Provenance to repair or retire. This is
  the same check `reflection_warnings` already makes at `scripts/workspace_lib.py:1369`, carried
  over unchanged onto the new fields.
- **A legacy `reflection.md` remains in the workspace root.** Migration is unfinished, which is a
  fact worth reporting rather than a fault worth blocking on.
- **`<project>/memory-staging.md` is non-empty at close.** Triage is unfinished; the project's
  actual deliverables may well be done.

And one non-finding, stated because it is load-bearing:

> **A workspace root with no `MEMORY.md`, no `POSTMORTEMS.md` and no `memory/` directory is valid,
> and so is every project in it, with or without a `memory-staging.md`.**

Every project created before this architecture existed must stay valid and stay reopenable. That is
the same reason `briefing.md` is absent from the closure requirements
(`references/workspace-schema.md:250`), and it is why `schema_version` stays at **3**: nothing
about canonical project state changes here. Memory lives beside projects, not inside their schema.

## Root layout

```text
workspace/
├── INDEX.md                 # Generated: projects, canonical status
├── MEMORY.md                # Generated under .index.lock; 120 lines / 12 KB
├── POSTMORTEMS.md           # Generated under .index.lock; no budget, read on demand
├── memory/
│   └── <slug>.md            # One topic; amended under .memory.lock; any length
└── YYYY-MM-DD-NNN/
    ├── project.json         # Canonical state; unaffected by this design
    ├── evidence.md          # Appended under .project.lock
    ├── memory-staging.md    # Appended under .project.lock; drained at close
    ├── ...
    └── reflection.md        # Project post-mortem: unchanged format, indexed by POSTMORTEMS.md
```

`research-project init` scaffolds `MEMORY.md` and `memory/` — the same place the flat file was
created, at `scripts/workspace_lib.py:1988` — and adds either if missing from an existing root. It
scaffolds `memory-staging.md` into each new project skeleton instead, beside `evidence.md`. Nothing
is created anywhere else, and no absence is ever repaired implicitly by validation.

`MEMORY.md`, `POSTMORTEMS.md` and `memory/` live under the `workspace_root` output root
(`references/workspace-schema.md:137`): they are shared records belonging to no single project, so
a task that writes one declares it there. `memory-staging.md` belongs to its project and is
declared under `workspace`, exactly like `evidence.md`.

## Migration

The 15 existing entries move by hand, once:

- Each bulleted entry and each dated `###` cluster becomes one topic file.
- `kind` comes from the `##` section the entry sat under: `## Confirmed user preferences` →
  `preference`, `## Environment and tooling` → `environment`, `## Method` → `method`.
- `sources` and `scope` are copied **verbatim** from the entry's bracketed metadata or `Source:`
  lines. Provenance is the one thing a migration must not paraphrase.
- Bodies move whole. Merging two entries is an editorial decision to make deliberately and
  separately, not a side effect of moving files.
- Then `MEMORY.md` is generated and must land inside budget. Fifteen topics is about 15 lines of
  pointers, so it will.

The flat `reflection.md` is deleted only after the topic files exist and the generated `MEMORY.md`
validates. Its content stays recoverable from version control, which is why `init` and
`research-validate` warn about a workspace root that is not under version control (`vcs_warnings`,
`scripts/workspace_lib.py:1272`) — that warning is what makes this deletion recoverable rather than
final.

Until a root is migrated, the leftover `reflection.md` produces a warning and nothing else. No root
is migrated implicitly.

## Deliberately excluded

Two prior architectures informed this design, and most of what makes each of them powerful is
absent here. The reason is almost always the same, so it is worth stating once:

> A shared plugin change must work in **Claude Code and Codex** (`AGENTS.md:41`), and
> shipped scripts must run on stock macOS **Python 3.9** using **only the standard library**
> (`AGENTS.md:74`). Anything that needs a host-specific hook, a background process, a model, or a
> compiled extension either works on one host or works nowhere.

**A vector index, embeddings, or semantic search.** Needs a model and either a background daemon or
a compiled extension. Lexical matching over `description` and `scope` is what remains available on
both hosts, which is precisely why those two fields carry the retrieval weight.

**SQLite FTS5 with `sqlite-vec`.** FTS5 is not guaranteed in a stock `python3` build and
`sqlite-vec` is a compiled extension. A search index that fails to open on one host is worse than
no index, because its absence is discovered at the moment it is needed.

**`PreToolUse` and `PostToolUseFailure` hooks.** Hook configuration is host-specific and per-user;
a plugin cannot rely on it existing. The per-project staging step replaces what hooks would have
automated, at the cost of being explicit rather than automatic.

**A warm embedding daemon.** A background process is outside what a plugin may install, and its
absence would silently degrade retrieval rather than fail loudly.

**A retrieval subagent with its own disposable context.** Subagent definitions do not port across
the three hosts, and this repository's instructions forbid spawning agents unprompted.
`search-memory` does the same job in the caller's context, and printing paths rather than contents
is what keeps that context cost bounded.

**A specialist curator agent with bucket routing.** The same portability problem. Triage happens at
close, done by the coordinator, with a warning when it is left undone.

**A session corpus with a `retrieval_failures` health metric.** Requires capturing every session,
which requires hooks. This is the most valuable excluded idea and the first one worth revisiting if
hooks ever become portable across the three hosts.

**Automatic clustering or merging of topic files.** Merging is an editorial judgement about
meaning. Under this design the budget makes the pressure to merge visible at the moment it matters,
and a person merges. This is also why the budgeted file may hold only what merging can fix: pressure
a reader cannot relieve by any act the design offers is not pressure, it is a wall.

**A `schema_version` bump.** Nothing in canonical project state changes here. Bumping it would
invalidate every existing project in order to add a file beside them.

What both prior architectures converged on is what survived here, and it is the whole design: layer
by load frequency, budget the discovery layer, keep the substrate plain text a human can read
and `grep`, index by pointer rather than by summary, make retrieval explicit, and never let
advisory memory block canonical state.
