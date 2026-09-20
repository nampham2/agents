# Project workflow automation and optional MCP — implementation plan

Status: primary CLI implementation completed on 2026-09-20; local MCP deferred after the measured gate.

Prepared on 2026-09-20 against the repository's 0.12.0 implementation. This document is a handoff
for implementation agents. Recheck the source and worktree before implementing; later user
instructions and `AGENTS.md` take precedence. Writing this plan does not authorize publishing,
committing, installing a plugin, or modifying an existing research workspace.

## 1. Objective and implementation boundary

Make the project skill faster and cheaper to operate by moving repeated mechanical work into
tested Python functions and exposing useful operations through the existing command launchers.
Design those functions so an optional MCP adapter can call them directly later.

The user's observed problem is that agents write too much ad hoc Python to navigate projects,
modify Markdown, construct state patches, and record results. Optimize the complete interaction:
the instructions an agent reads, the inputs it authors, the commands it invokes, the results it
receives, and the retries it needs. Python execution time is a separate measurement.

Deliver the following in the primary implementation:

1. A reproducible baseline and comparison of representative project lifecycles.
2. Shared, transport-independent operations for updates, task transitions, document maintenance,
   evidence selection, validated resume, and closure.
3. CLI access through the existing Claude Code and Codex launchers.
4. Skill instructions that use these operations for routine work.
5. Tests, compatibility checks, and a measurement report with honest limitations.
6. A documented decision about whether an MCP prototype would add enough value to justify itself.

The optional MCP stage is conditional. Do not implement a server merely because the preceding
work is finished. Compare it with the improved CLI, not only with the current implementation.
Any change to the repository's dependency or supported-runtime policy needs a separate explicit
decision. Once the primary plan is approved, its ordinary implementation phases can proceed
without asking for approval again at each phase.

Out of scope for the primary implementation:

- Replacing `project.json`, Markdown records, or the v3/v4 state schemas with a database.
- Rewriting the executor, introducing background workers, or changing delegation defaults.
- Automatically deciding success criteria, granting authorization, or accepting a task because
  one command returned zero.
- Mandatory reports, interviews, reviews, memory promotion, or new project ceremonies.
- Remote hosting, HTTP authentication, dashboards, or MCP resources/prompts without a measured need.
- Global filesystem search, plugin-cache discovery, or changes to live projects for benchmarking.
- Automatic execution of a task's free-text `verification` field.

## 2. What is already implemented

Read these files before making overlapping changes:

| File | Relevant behavior |
| --- | --- |
| `plugins/research/skills/project/SKILL.md` | Current default lifecycle and optional-reference routing. |
| `plugins/research/skills/project/references/commands.md` | Patch semantics, context retrieval, evidence and authorization conventions. |
| `plugins/research/skills/project/scripts/workspace_session.py` | Bounded context, task-only projections, Markdown heading scanning, selected reads, project discovery, patch merge/defaulting. |
| `plugins/research/skills/project/scripts/workspace_lib.py` | State/file validation, locks, atomic writes, evidence execution, candidate checks, guarded commits, index rebuilding. |
| `plugins/research/skills/project/scripts/manage_workspace.py` | CLI parser and dispatch. |
| `plugins/research/skills/project/scripts/validate_workspace.py` | Standalone validation CLI. |
| `plugins/research/skills/project/references/task-workers.md` | Native-worker ownership notes and coordinator-only shared writes. |
| `tests/plugins/research/project/test_workspace_session.py` | Update, revision, context, and authorization regressions. |
| `tests/plugins/research/project/test_context_retrieval.py` | Lossless pagination, fence-aware reads, evidence ownership, launcher coverage. |
| `tests/plugins/research/project/test_record_evidence.py` | Actual command execution, path mistakes, shell operators, append durability. |
| `tests/plugins/research/project/test_lifecycle_payloads.py` | Existing before/after retrieval-payload benchmark and instruction budgets. |
| `docs/project-overhead-review.md` | Previous observations, measurements, and their limitations. |
| `AGENTS.md`, `pyproject.toml`, `.github/workflows/ci.yml` | Host contracts, Python compatibility, coverage, tooling and release rules. |

Important existing capabilities to retain:

- `context`, task-only/worker views, paged `read`, and filtered `list-projects` already reduce reading.
- `update` already merges small patches, defaults task boilerplate, derives `current_tasks`, and
  routes through guarded commits. It currently requires an input file.
- `record-evidence` already executes the real command in the target directory and durably appends
  actual results. It returns an integer to Python callers and prose to CLI callers.
- Evidence appends already use the project lock and atomic replacement. Do not reimplement them
  as unlocked appends or plain truncating writes.
- `commit_candidate` checks revision, terminal-history immutability, transitions, ownership,
  authorization, outputs and evidence, then writes `project.json` and rebuilds the index.
- An index failure after a commit is explicitly different from a rejected commit.

Limits that matter to the new design:

- Current evidence-reference validation checks reference shape and file existence. It does not
  prove an anchor exists, a particular command passed, or the evidence applies to current outputs.
- `parse_evidence_spans` aggregates historical timing/counts. It is not an acceptance-record parser.
- Document writes are not versioned by the `project.json` revision. A project revision alone cannot
  detect a concurrent manual edit to `spec.md`.
- `execution_active` describes executor records; it does not identify native workers. The skill's
  native-worker recovery procedure remains necessary.
- Current lifecycle tests keep the number of observations equal and compare returned bytes.
  They do not establish total token, cost, or end-to-end latency savings.
- The earlier overhead review rejected MCP for that change. This plan revisits the question only
  after operations improve and invocation overhead can be measured independently.

## 3. Non-negotiable compatibility and correctness requirements

### 3.1 Runtime and installation

Shipped core code must be stdlib-only and run with Python 3.9. Development tools run with Python
3.11+. Follow the repository's existing typing conventions, including guarded imports where needed.
Public functions must have type annotations; project state remains `dict[str, Any]`.

Do not introduce an importable `agents` package or a build/install requirement for ordinary CLI
operations. Both host launchers must call the same implementation:

- Claude Code: `plugins/research/bin/research-project` and `research-validate` through `PATH`.
- Codex: the corresponding executable launchers beside the loaded project skill.

Skills must not call Python implementation files directly, locate caches, infer the installed
plugin root from the working directory, or depend on a session-shell `CLAUDE_PLUGIN_ROOT`.

### 3.2 State and evidence

Preserve v3/v4 compatibility, revision checks, task dependency semantics, authorization, external
receipts, immutable terminal history, executor ownership refusal, and existing migration behavior.
Do not manufacture consent. A dependency must be `DONE`; `SKIPPED` is not equivalent.

`project.json` remains the state commit point. Write it last when an operation prepares supporting
documents. Route post-commit index rebuilding through `_rebuild_index_after_commit`. Never retry
an already committed mutation as if the index failure had rejected it.

Command evidence comes from actual execution. Findings, decisions, worker summaries, and acceptance
judgments are prose records, not executable verification evidence. Keep failed checks visible.
Selecting a later passing check must not erase an earlier failure.

### 3.3 Files and errors

Use existing `read_text`, atomic-write helpers and locks. Malformed JSON, Unicode/decode failures,
bad input types, missing files and filesystem errors must produce actionable errors/findings,
not tracebacks from unchecked traversal.

New document commands address a fixed set of project documents. They are not arbitrary file-write
tools. Check path containment and symlinks before replacing a managed document; reject a managed
path that resolves outside the intended project. Never interpolate a task ID into a filesystem
path without checking it is a safe single path component and resolves under `tasks/`.

The scripts serialize cooperative writers. They cannot guarantee protection against an unrelated
process that edits files without taking the project lock; document this boundary accurately.

## 4. Architecture: one implementation, thin interfaces

Keep semantic operations independent of argparse, stdin/stdout, shell quoting, and MCP types.
They accept Python values and return structured values or raise existing workspace errors.
CLI wrappers parse/format; a future MCP wrapper validates its request and calls the same functions.

Suggested file allocation:

| File | Responsibility |
| --- | --- |
| `workspace_session.py` | Existing retrieval and patch logic; add mapping-based update entry point and optional validated context. |
| `workspace_operations.py` (new) | Task transition and closure composition using existing commit guards. |
| `workspace_documents.py` (new) | Fence-aware section selection, outlines, content tokens, document edits and prose appends. |
| `workspace_evidence.py` (new, if separation is useful) | Structured record metadata, entry selection and acceptance checks; avoid circular imports with `workspace_lib`. |
| `workspace_lib.py` | Existing primitives; extract a shared in-memory commit path only where needed to avoid temporary candidate files and lock gaps. |
| `manage_workspace.py` | Additive CLI parsing, compact result rendering and error presentation. |

These are module boundaries, not an instruction to split every helper. Keep pure evidence parsing
in a module that does not import `workspace_lib` if `workspace_lib` needs to call it. Avoid a new
generic workflow framework, plugin registry, dependency injection system, or service container.

A safe initial refactor is:

1. Extract patch application into a function that accepts a mapping and does not mutate its input.
2. Retain `update_project(project_dir, patch_path, ...)` as a compatible wrapper.
3. Retain `commit_candidate(project_dir, candidate_path, ...)` as a compatible wrapper if an
   in-memory commit function is added.
4. Keep all commit guards in one implementation. Reuse that implementation for task helpers.
5. Perform evidence selection and the accepting commit under the same project lock. Use a narrowly
   scoped internal locked helper rather than recursively acquiring `DirectoryLock`.
6. Keep index rebuilding outside the project lock as today; preserve existing lock ordering.

Do not introduce a second validation path that gradually diverges from the original commands.
Public operations should accept explicit `expected_revision` for state mutations, and explicit
document content tokens for replacement edits. MCP must not supply a current revision silently.

## 5. Proposed CLI contract

The commands below are new interfaces to implement, not commands available in 0.12.0. Names may
be adjusted during implementation if an existing parser conflict requires it; keep the skill,
help, tests and this plan's final disposition synchronized.

Use absolute project paths in examples. Quoted heredocs are acceptable for passing JSON/text
through stdin; they are data input, not ad hoc Python. Never use shell expansion to construct
untrusted bodies or paths.

### 5.1 Structured results and backward compatibility

Existing commands retain their current default output unless an explicit option selects a new
format. Add `--json` to existing mutations where needed. New commands default to a compact JSON
result so an agent can continue without parsing prose or reloading the full state.

Successful state mutation results should include:

```json
{
  "operation": "task.finish",
  "project_directory": "/absolute/project",
  "committed": true,
  "revision": 5,
  "status": "EXECUTING",
  "changed_tasks": [{"id": "T01", "status": "DONE"}],
  "current_tasks": [],
  "warnings": []
}
```

Return changed task summaries, not the complete project or all evidence. Reuse paged reads for
large error details. Do not silently truncate error findings; return totals and a continuation
mechanism if bounding them becomes necessary.

Structured failure results distinguish at least:

- Bad request or invalid state: nothing was committed.
- Revision/document conflict: nothing from that requested mutation was committed; report the
  current token/revision and the relevant reread operation.
- Command failed: real result was recorded if recording succeeded; return its actual exit code.
- Command ran but recording failed: side effects may have occurred; do not advertise a safe rerun.
- State committed but index maintenance failed: report committed revision and index recovery.
- Closure reflection saved but state not committed: report the changed document and current state.

Carry commit/recording outcomes as typed result fields or workspace-error subclasses with attributes.
Do not recover `committed`, revision, or command outcome by parsing English exception messages.
Preserve the existing base exception contract for old callers. When a filesystem failure occurs
after replacement and durability is uncertain, return an explicit unknown outcome and a reread
instruction instead of asserting `committed: false` or `recorded: false`.

Preserve existing CLI exit behavior for old invocations. In JSON mode write machine output to
stdout and diagnostics to stderr; command output belongs in recorded evidence, not interleaved
with the JSON envelope. Do not globally change exit codes without compatibility tests.

### 5.2 Patches without input files

Extend `update` to accept `-` as `patch_json`:

```sh
research-project update /absolute/project - --expected-revision 2 --json <<'JSON'
{"status":"PLANNING","tasks":[{
  "id":"T01",
  "name":"Implement and verify parser fix",
  "success_criteria":"Malformed input produces a finding without crashing",
  "verification":"Run parser regressions and repository checks",
  "effect":{"kind":"local_write","description":"Edit parser and tests"},
  "outputs":[{"root":"target","path":"src/parser.py","required":true}]
}]}
JSON
```

Use exactly the existing object-merge, list-replacement, task-by-ID and defaulting semantics.
Reject non-object JSON, unsupported fields, duplicate task updates and invalid task definitions.
Do not add a parallel task-planning DSL or a large collection of flags for every schema field.
One compact JSON plan is reasonable agent-authored data.

### 5.3 Task transitions

Provide:

```text
task <project> start <task-id> --expected-revision N
task <project> finish <task-id> --evidence <record-id> [--evidence <record-id> ...]
     [--start-next <task-id>] --expected-revision N
task <project> block <task-id> --reason <text> --expected-revision N
task <project> skip <task-id> --reason <text> --expected-revision N
```

Semantics:

- `start` sets the selected task to `RUNNING`, clears inapplicable block/skip reasons, derives
  `current_tasks`, and moves the project to `EXECUTING` only if that transition is legal.
- Starting does not select a task automatically, authorize it, or resolve unknown worker ownership.
- `finish` is an explicit coordinator acceptance action. It selects recorded checks, constructs
  evidence references, merges them with existing references without duplication, and commits `DONE`.
- A selected executable check must belong to this task and have a reliably parsed zero exit code.
  Do not silently select the latest check or accept an unrelated task's result.
- `--start-next` combines completion and an explicitly chosen successor start in one guarded
  candidate. If either transition is invalid, neither task transition commits.
- `block` and `skip` require nonempty reasons. Follow existing legal transitions; do not invent
  implicit project-wide status changes for these operations.
- Helpers never modify effect descriptions, authorization, receipts, success criteria, or outputs
  as a side effect. Existing `update` remains available for those explicit edits.
- External completion still requires a receipt already recorded through an authorized update.
- Task completion without executable checks, such as an accepted research artifact, remains
  supported through `update` with explicit evidence references. Document this route; do not force
  agents to run meaningless commands to satisfy the convenience helper.

Preserve retry safety: a stale expected revision does not become an implicit successful retry.
If a caller loses the response, it retrieves current context and reconciles what happened.

### 5.4 Document discovery and content tokens

Extend document reads with an outline mode and a content token:

```text
read <project> spec --outline
read <project> spec --section "Constraints and important assumptions"
read <project> reflection
read <project> notes --task T01
```

Return `document_sha256` for the whole document, independent of a selected section/page. A missing
optional document has `exists: false` and a documented creation token such as `missing`.
The outline returns heading levels/titles and explicit ambiguity information without every body.
Page large outlines using existing offset/limit conventions with names documented unambiguously.

Reuse or extract the existing fence-aware `_headings` scanner. Do not use a regex that treats
headings inside code fences as document structure. Specification selection must stay within
`Current specification`, except an explicit read of `Decision history`.

State revision and document digest answer different questions. Never claim that a matching state
revision proves a document has not changed. For replacement writes, compare the whole-document
digest after taking the project lock; reject a mismatch without replacing the file.

### 5.5 Specification and reflection edits

Provide targeted and batched edits:

```text
edit <project> spec --section <exact-heading> --body-file <path-or-dash>
     --expected-sha256 <digest>
edit <project> spec --sections-json <path-or-dash> --expected-sha256 <digest>
edit <project> reflection --body-file <path-or-dash> --expected-sha256 <digest-or-missing>
```

`--sections-json` is a mapping of exact headings to replacement body strings. It replaces several
sections in a single file write, so initial specification authoring need not take seven commands.
Support mutually exclusive `--body <text>` and `--body-file` for short/long text where useful.

Replacement requirements:

1. Resolve the allowlisted document and load/validate project state.
2. Acquire the project lock, recheck current state/ownership, and compare the content token.
3. Require an exact unique heading in the permitted region. Refuse missing/ambiguous headings
   with the available headings; do not guess or append a duplicate section.
4. Replace only the section body, retaining its heading and all content outside its range.
5. For a batch, resolve all ranges against the original text before changing any bytes. Reject
   overlapping parent/child selections rather than depending on patch order.
6. Reject replacement bodies containing unfenced headings at or above the selected section level.
   Lower-level headings and fenced examples are allowed.
7. Preserve the original newline style and text outside replacement spans. Test CRLF as well as LF;
   use an appropriate read helper for exact document preservation rather than universal-newline
   normalization followed by a claim of byte preservation.
8. Write the resulting document once through atomic replacement and return the new content token.

Reflection replacement is explicitly a whole-document operation, not a Markdown rewrite of user
sections. A missing reflection may be created with the creation token. Empty closing content is
not accepted. The agent supplies the outcome, limitations, and next steps; scripts do not fabricate
these from task statuses.

Document-only operations do not bump `project.json` revision. They must check executor ownership
under the project lock and retain coordinator-only usage in skill instructions. Do not pretend
that the tool can infer which native agent is calling it.

### 5.6 Decisions and findings

Provide:

```text
append <project> decision --body-file <path-or-dash> [--entry-id <id>]
append <project> finding --task T01 --body-file <path-or-dash> [--entry-id <id>]
```

- Decisions append within `spec.md`'s unique `Decision history` section.
- Findings append to `tasks/<id>.md`; verify the task exists and its filename is safe.
- Existing native-worker launch/recovery notes must remain intact. A finding append does not
  replace a whole task note or set a worker state.
- Each entry has a generated timestamp and identifier. Return the identifier, path, anchor and
  updated document token. Timestamp and ID generation belong in code.
- Appends read the latest document under the lock and therefore need no content token by default.
- An optional caller-provided `entry-id` enables idempotent retry: same ID and content returns the
  existing entry; same ID and different content is a conflict. Scope IDs to the destination document.
- Only consider identifiers in generated metadata positions outside fences; example text must
  not be mistaken for an existing entry. Choose a small documented ID character set.
- If the caller did not retain/supply an ID and loses the response, it must inspect records before
  retrying; do not promise universal exactly-once appends.
- These records are labeled prose. They never acquire an exit code or become a passing check.

Avoid a new mandatory findings file or universal log. Optional task notes and the existing decision
history are sufficient for the first release.

### 5.7 Structured command evidence and explicit selection

Extend the existing runner, preserving its command-execution and durability behavior:

```text
record-evidence <project> --task T01 --json -- <command> <arguments...>
record-evidence <project> --step report --json -- <command> <arguments...>
read <project> evidence --task T01 --entries
```

The structured result contains at least:

```json
{
  "operation": "record-evidence",
  "recorded": true,
  "record_id": "ev-0123456789abcdef0123456789abcdef",
  "owner": {"kind": "task", "id": "T01"},
  "exit_code": 0,
  "passed": true,
  "working_directory": "/absolute/target",
  "reference": {
    "root": "workspace",
    "path": "evidence.md",
    "anchor": "evidence-0123456789abcdef0123456789abcdef"
  }
}
```

Generate an identifier for each new recorded entry and add an explicit HTML anchor plus a small
metadata field to that entry in `evidence.md`. Keep existing owner headings (`## T01 — ...`),
timestamps, exit-code lines and output fences compatible with current readers and task graphs.
No sidecar becomes a second source of truth, and no state-schema change is needed.

Implement a structured runner result internally; keep the existing `record_evidence(...) -> int`
API as a wrapper for current callers. Do not execute the command twice to obtain both forms.
Only return `recorded: true` after durable append succeeds.

The entry reader returns a bounded list of IDs, ownership, timestamp, exit code, command excerpt,
and reference, with pagination. Output bodies remain available through existing selected reads;
add an exact `--entry <record-id>` selector if needed to inspect a failure without all task history.
A task/step filter and an entry selector must agree if both are supplied.

Evidence parsing requirements:

- Parse generated metadata outside fenced stdout/stderr; output cannot manufacture passing checks.
- Reject duplicate record IDs and malformed/ambiguous exit-code metadata for convenience completion.
- Validate the explicit anchor exists for new selectable records.
- Preserve failed records when later checks pass. The entry list exposes both.
- Legacy records without IDs remain readable and valid under existing reference rules. Label them
  as legacy/unselectable by ID; use the existing explicit-reference update path to reuse them.
  Do not force a command rerun solely to obtain an ID and do not silently rewrite historical evidence.
- Content freshness is a coordinator judgment in this release. The record timestamp/ID does not
  establish that checked outputs are unchanged. Do not claim content-addressed provenance.
- The convenience `finish` checks selected records under the same lock as its commit; do not accept
  records based on a stale read followed by an unrelated commit.

Preserve mutually exclusive task/closure-step ownership. `report` is a closure step, not an invented
task. Do not add new `CLOSURE_STEPS` values merely to make helper commands easier to implement.

Keep timeouts and execution errors honest: the current implementation does not append a completed
result after a timeout or launch failure. Structured output must not invent an exit code or promise
that descendant processes stopped. Process supervision and streaming output are separate work.

### 5.8 Validated resume and navigation

Add `context <project> --validate` as an additive mode. It combines the existing bounded context
with the same findings returned by `validate_project`, including filesystem checks. Return a
nonzero status when validation has errors; warnings remain distinguishable from errors.

Include roots and a small document map: existing managed documents, a specification content token,
and how to request an outline or missing section. Do not embed every task note, evidence body,
heading, or decision-history entry in ordinary resume context.

Avoid duplicate independent full-state reads where practical, but correctness wins over a micro
optimization. For cooperating writers, obtain context and validation from a consistent snapshot
under a bounded project-lock acquisition or explicitly detect revision changes and report a retry.
Do not run external commands while holding this read/validation lock. Explain that external target
files can still change outside the project's locking discipline.

Do not silently change legacy `context` or `--task-only` behavior. Task-only reads must retain full
selected requirements and must continue to avoid reading unrelated specification/evidence bodies.
Disallow incompatible flags with clear errors rather than quietly expanding a task-only payload.

Do not add heuristic advice about which task the agent should choose. Existing ready IDs and actual
validation findings are sufficient. `--validate` is not permission to start work.

### 5.9 Closure composition and recovery

Provide:

```text
close <project> --expected-revision N
close <project> --expected-revision N --reflection-file <path-or-dash>
      --expected-reflection-sha256 <digest-or-missing>
```

The first form uses an already written reflection. The second composes guarded reflection writing
with the same state transition. Both commit `DONE` only through existing validation/commit guards,
then run closure validation with `check_index=True` and return the findings.

Do not quietly finish outstanding tasks, waive required reviews, create receipts, write a report,
or invent a reflection. Require a fresh expected revision and any necessary reflection token.

This is not an all-or-nothing transaction across Markdown and JSON. Use an explicit staged contract:

1. Validate arguments, state revision, supported schema and ownership before changing a document.
2. If supplied, write the reflection with the document helper and remember its new token.
3. Attempt the guarded state commit using the caller's expected revision. Concurrent changes or
   remaining closure errors can still reject it.
4. If rejected, the saved reflection is retained as a draft; report `reflection_saved: true`,
   `committed: false`, its token and the actual state. Do not delete or roll back someone else's edit.
5. If JSON was committed, report that fact even if index rebuilding or final validation fails.
6. After an index-only failure, instruct `rebuild-index` and closure validation, not another `close`.

Where cheap, check obvious task/review blockers before writing the reflection, but do not create
a second authoritative closure validator. A draft reflection remaining after failure is acceptable
and must be tested/documented. The state commit remains last among authoritative project writes.

## 6. Measurement design

Add tests and a reproducible harness before changing the hot paths. Extend the current lifecycle
test or add `tests/plugins/research/project/test_workflow_automation.py`. Keep executable benchmark
fixtures with tests, rather than introducing a runtime benchmarking dependency.

### 6.1 Scenarios

| Scenario | Required actions |
| --- | --- |
| New project | Initialize v4, fill all specification sections, append a decision, plan three dependent tasks, execute/check/finish, write reflection and close. |
| Resume with history | Load 200 completed tasks plus active work, retrieve required specification details, change one section, continue and close. |
| Failure and correction | Record a real failing check, inspect its output, correct a fixture output, record a pass, explicitly finish; retain both results. |
| Interrupted mutation | Inject revision conflict, document conflict, evidence-write failure and post-commit index failure; exercise documented recovery. |
| Prose findings | Append findings to a task with existing worker notes; preserve those notes and verify idempotent append behavior. |
| Compatibility | Run representative new operations on v3 and v4 through each launcher surface. |

Temporary fixture targets should be deterministic, small and harmless. The baseline and improved
flows must produce equivalent requested artifacts and task outcomes, allowing only documented
additive record metadata. Do not suppress useful verification in the improved flow.

Use the current documented lean workflow as the baseline, including task-only reads and batched
updates where already available. Do not inflate it with avoidable full-state reads, mandatory
dry runs, or separate transitions that the existing CLI already batches. Freeze the baseline
scenario specification and count inputs as well as results before implementing the new commands.

### 6.2 Metrics

Record separately:

- Semantic operation count, CLI process invocations, and agent/host tool round trips. A shell
  command containing three CLI invocations is one host call but three process invocations.
- Input bytes and output bytes, including authored patches, document bodies and retry inputs.
- Instruction words/bytes loaded on the tested path, including command references.
- Agent-authored temporary files and ad hoc bookkeeping-code bytes.
- Largest individual result, and explicitly omitted/truncated content that must later be retrieved.
- Python/process runtime, measured with repeated trials and reported as a distribution or median.
- Failed invocations, rereads and recovery steps.

Normalize only incidental paths, timestamps and IDs consistently. Count required reflection/spec
prose in both flows. Separate task-implementation code from bookkeeping code so the measurement
does not penalize a task whose actual deliverable is Python.

Synthetic harnesses measure the documented workflow, not model reasoning. If authorized native
agents and usage reporting are available, add a controlled behavioral comparison using the same
request, model/configuration, fixture and success checks. Report total coordinator-plus-worker
tokens, wall time and actual billed/cache categories only when exposed. Record unavailable metrics
as unavailable; never convert bytes/4 into claimed billing savings.

Do not add timing assertions to CI. Use deterministic assertions for payload/interaction properties
and report timing separately. Do not require another paid agent run when no such capability is
available; make that limitation visible.

### 6.3 Acceptance targets

The primary release must demonstrate:

1. No ad hoc Python or shell text-rewrite program is needed for ordinary project bookkeeping.
2. No agent-authored temporary transition patch/candidate files are needed on the new path.
3. All seven initial specification sections can be written in one guarded edit.
4. An explicit task finish can attach recorded evidence and optionally start a named successor in
   one state mutation, without reloading the full project.
5. Resume context plus validation can be retrieved in one invocation.
6. Small and resumed scenario bookkeeping invocations decrease against the current lean workflow.
   Treat 25% as a design target, not a fabricated result or a reason to remove required checks.
7. Aggregate input/output payload does not regress materially without a documented correctness
   benefit. Include new tool/help/schema text in relevant comparisons.
8. Failed checks and all important guards remain demonstrably effective.

If a convenience command increases complexity without reducing measured work, remove or simplify
it before release. Keep the underlying useful operation where independently justified.

## 7. Implementation phases and handoff tasks

These phases are a dependency order, not mandatory separate approval ceremonies. Each phase should
leave a reviewable change and record actual verification. Do not commit unless asked.

| ID | Work | Depends on | Exit evidence |
| --- | --- | --- | --- |
| P01 | Capture baseline fixtures, metrics and current behavior. | None | Reproducible existing-flow outputs; no claimed improvements yet. |
| P02 | Extract mapping-based update/commit functions; add stdin and compact mutation results. | P01 | Compatibility tests pass; old wrappers behave as before. |
| P03 | Implement document tokens, outlines, edits, decision/finding appends. | P02 where shared errors are used | Preservation, fencing, conflicts and interruption tests pass. |
| P04 | Add structured evidence records and task helpers. | P02 | Real pass/fail selection, atomic acceptance and transition tests pass. |
| P05 | Add validated context and closure composition. | P03, P04 | End-to-end close/resume and partial-failure recovery pass. |
| P06 | Integrate skill/examples, run comparisons, complete release checks. | P01–P05 | Measured report and full repository checks. |
| P07 | Decide whether a local MCP prototype is justified. | P06 | Written comparison of remaining costs and dependency feasibility. |
| P08 | Conditional MCP prototype and host trials. | Positive P07 decision and applicable authorization | Same semantics, both host trials, measured comparison with improved CLI. |

P03 and P04 can be investigated or implemented independently after agreeing on P02 interfaces.
If multiple agents work, give one owner to `manage_workspace.py`, shared commit primitives and
skill integration; use separate modules/tests for other work. Do not have independent agents
rewrite the same parser or transaction path concurrently.

For every phase handoff, report changed files, concrete behaviors, test commands/results, unresolved
limitations, and the next phase's prerequisites. Do not replace failed command output with prose
claiming success. If this implementation itself uses a research workspace, record its checks with
the resolved `research-project record-evidence` launcher.

## 8. Test matrix

Add behavior-focused tests under `tests/plugins/research/project/`. Suggested files are
`test_workspace_documents.py`, `test_task_operations.py`, `test_evidence_records.py`,
`test_close_operation.py`, and `test_workflow_automation.py`. Reuse existing fixtures where their
defaults do not hide the behavior under test.

| Area | Required cases |
| --- | --- |
| Input/update | File and stdin equivalence; malformed JSON; non-object JSON; bool/negative revision; unsupported fields; input mapping unchanged. |
| Commit | Stale revisions; terminal history; dependency not DONE; authorization pending; missing output; missing receipt; active executor; index failure after commit. |
| Task helpers | Start; unblock/start; finish with selected checks; finish/start-next all-or-nothing; block/skip reasons; illegal transitions; repeated stale call. |
| Sections | Nested headings; fenced fake headings; duplicate names; missing heading; Unicode; LF/CRLF; no final newline; batch overlap; decision-history boundary. |
| Documents | Stale digest; absent/create token; same-content edit behavior; write failure before replace; symlink escape; malformed state; busy lock; ownership refusal. |
| Appends | Two cooperative appends preserved; task exists; unsafe task filename; existing notes preserved; supplied ID same-body retry; different-body conflict. |
| Evidence | Actual zero/nonzero commands; exact argv/cwd; task vs step; unique IDs/anchors; multiple checks; failed history retained; legacy entries; metadata-looking stdout. |
| Acceptance | Wrong task ID; unknown/duplicate record ID; missing anchor; malformed exit metadata; selected failed check; selected entry changed before commit. |
| Runner failure | Launch error; timeout; append failure after command ran; structured result never falsely reports a stored success; no automatic rerun. |
| Resume | Valid and invalid state; missing files; bounded context; lossless task requirements; explicit truncation; consistent revision; lock timeout. |
| Closure | Required reviews incomplete; unfinished task; invalid reflection token; saved draft after rejection; successful commit then index/final-validation failure. |
| Compatibility | v3/v4; both launcher paths; existing default CLI output; unchanged legacy evidence/graphs; Python 3.9 imports and actual new commands. |

Current coverage policy is 100% statement coverage for shipped project scripts. Cover meaningful
failure branches through injected faults rather than suppressing coverage for error handling.
Do not weaken instruction budgets or existing invariants simply to make tests pass.

The distinction between a replace failure and a failure after replacement/fsync also matters:
do not report “unchanged” after a write may already have landed. Tests should inspect actual files
and returned recovery guidance at durable boundaries, not merely assert that an exception occurred.

## 9. Skill and documentation integration

Update `SKILL.md` only once the commands exist and are tested. The skill should explain:

- How to resolve the same host-neutral launcher pair once.
- How to resume with bounded validated context and retrieve omitted relevant requirements.
- How to submit one compact plan, edit specification content, and append consequential decisions.
- How to start a task, run real checks, explicitly accept recorded evidence, and optionally start
  an already planned successor.
- How to save findings without pretending they are checks.
- How to close with supplied reflection content and handle partial success accurately.

Keep exact flags, input schemas, recovery examples and legacy routes in `references/commands.md`.
Update the skill README and relevant worker/schema references only where behavior changes.
Retain existing word budgets (`SKILL.md` <= 900 words; entry plus routine command reference <= 1500)
unless measurements support a documented revision. Put substantial new command detail in one
focused optional reference if needed; do not load every advanced procedure on the default path.

The skill-creator guidance informs this split: scripts own repeated transformations and fragile
mechanics; instructions retain the judgments and constraints the model needs. Do not copy this
implementation plan into the runtime skill or link it as a mandatory lifecycle read.

Perform a forward trial from the revised instructions with only a realistic request and a fixture
workspace. A trial must demonstrate successful closure and inspect resulting files, not merely show
that help output includes the new commands. Run a native-agent trial only if available/authorized;
otherwise label a scripted walkthrough as such.

## 10. Conditional MCP stage

### 10.1 Decision criteria

After P06, identify the remaining cost. MCP is a candidate when agents still spend material work
resolving executables, reading command syntax, quoting multiline arguments, or repairing malformed
invocations. It is less compelling if the improved CLI already gives reliable one-call operations
and the remaining time is substantive project reasoning or long verification commands.

Evaluate:

- Incremental tool-call/retry reduction beyond the improved CLI.
- Input/schema/instruction context cost, including how each host discovers and loads tool schemas.
- Server startup/connection failures and operational maintenance.
- Supported runtime/dependencies and installation through both marketplaces.
- The host's actual permission behavior for server file writes and subprocess execution.
- Whether the same operations remain usable in scripts, CI, and hosts without MCP.

Do not assume MCP is token-free, faster, sandbox-equivalent to a terminal tool, or a replacement
for the skill. Do not claim host tool-discovery behavior without checking the actual supported host
versions. Document a negative decision as a valid result; the primary implementation is still useful.

### 10.2 Runtime feasibility gate

The default core must retain stdlib-only Python 3.9. Before writing a server, check current official
SDK requirements and supported protocol versions. Record one explicit disposition:

1. A compatible adapter can satisfy the existing constraints with acceptable maintenance cost.
2. A separately optional adapter runtime/dependency policy is proposed for user approval, while
   the CLI/core remain compatible.
3. The adapter is deferred because its maintenance/dependency cost exceeds demonstrated benefit.

Do not casually implement a partial JSON-RPC server to avoid declaring a dependency. Even a local
server needs correct initialization/capability behavior, tool listing/calls, errors, framing,
shutdown, and host compatibility. A hand-maintained implementation needs explicit scope and tests.
No decision in this plan pre-approves changing the repository's runtime policy.

### 10.3 Prototype architecture and tool surface

If justified, use a local stdio adapter. The host starts it; no remote service, port, database or
authentication layer is needed for this prototype. Keep all authoritative state in project files.
Avoid cached mutable project state; reread under existing locks and use caller-supplied tokens.

Suggested small tool set:

| Tool | Shared operation |
| --- | --- |
| `project_list` | Existing bounded discovery; explicit workspace root/query/pagination. |
| `project_context` | Bounded context with explicit validation/task-only choices. |
| `project_read` | Selected document, section or evidence-entry retrieval. |
| `project_update` | Mapping-based guarded patch application. |
| `project_task` | Explicit start/finish/block/skip enum and corresponding arguments. |
| `project_document` | Explicit edit/append enum, document selector and content tokens. |
| `project_close` | Existing closure operation with expected revision and optional reflection. |

Do not expose a generic `run_shell` or arbitrary file-writing tool. Initial verification can remain
on the existing host shell via `record-evidence`, with MCP selecting its resulting records. This
hybrid path must be counted honestly in benchmarks. Only expose command execution through MCP if
there is a demonstrated benefit and host permissions, cancellation and long-running-command behavior
are explicitly tested. Reuse the real runner rather than accepting an agent-supplied claimed exit code.

Inputs use schemas with required fields, closed action enums and appropriate bounds. Keep the
number of tools modest without hiding the entire CLI behind a single untyped `execute` string.
Return the same structured results and error distinctions as the CLI. Distinguish protocol errors
from domain failures and keep successful stderr logs out of the JSON-RPC stdout stream.

Scope prototype filesystem access to explicitly configured workspace roots. A workspace's recorded
target path is untrusted data, not automatic permission to execute there. Follow host permissions
and existing effect authorization rules; an MCP connection does not confer task consent.

### 10.4 Packaging, tests and adoption

Consult current official host documentation before changing manifests/configuration. Do not guess
that Claude and Codex use identical MCP declaration formats. Install only through supported
marketplace/plugin workflows when installation is authorized; never copy or symlink directly into
host-managed plugin locations.

Test protocol behavior, malformed requests, input/output schemas, domain error mapping, process
startup/shutdown, concurrent calls, stdout purity and lost responses. Reuse semantic tests against
CLI and MCP adapters; the MCP tests must not contain another implementation of the business rules.

Run actual tool discovery and a small lifecycle in both hosts if the environment permits. A
stdio test client or launcher test is useful but does not establish live host compatibility. Report
an unavailable login/runtime as an uncompleted test, not a pass.

Compare current CLI, improved CLI and MCP adapter using the same fixtures and success criteria.
Keep MCP optional until incremental benefit and both host integrations are demonstrated. If adopted,
the skill selects the available interface once and preserves a documented CLI fallback. A write with
an uncertain MCP response must be reconciled from disk before attempting the CLI fallback.

Official references to recheck when implementing this conditional stage:

- [MCP tools and structured results](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
- [MCP transports, including local stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
- [Codex MCP configuration](https://developers.openai.com/codex/mcp/).

These are starting references, not a requirement to freeze the adapter to that protocol revision.
Record the actual compatible protocol/runtime/host versions used for the prototype.

## 11. Verification and release handoff

Run focused tests during implementation, then the required repository-wide checks once the final
code/docs are integrated:

```sh
uv run pytest -q
uv run ruff check .
uv run ty check .
uv lock --check
uv run pytest -q --no-cov tests/plugins/research/test_plugin_versions.py
```

Exercise new commands under Python 3.9 using the actual launchers; extend the existing CI smoke
coverage to reach meaningful new operations, including stdin, document edits and task transitions.
Keep tests' import paths and `pyproject.toml` type-checker paths synchronized if directories change.

Use the available skill validator on the modified project skill. After manifest changes, run:

```sh
claude plugin validate --strict .
claude plugin validate --strict plugins/research
```

For a release, select the next appropriate plain SemVer relative to the version at implementation
time; 0.13.0 is the candidate if 0.12.0 is still current. Update `pyproject.toml`, both plugin manifests
and the `agents` lock entry together, using `uv lock` and the focused version check. Do not bump
versions for this plan-only document. Do not introduce a development cachebuster into committed
manifests.

Installed copies are independent of source changes. Report that a host update/reinstall is needed;
do not perform it without authorization. Preserve unrelated worktree changes and do not commit,
push or publish merely because implementation checks passed.

The final implementation handoff must include:

- The operations actually delivered and a short executable lifecycle example.
- Before/after measurements with baseline definition and metric limitations.
- Exact tests run, pass/fail status, coverage, and any uncompleted host/runtime trial.
- Compatibility or behavior changes, including additive evidence metadata and partial-close behavior.
- Remaining known limitations, especially evidence freshness and native-worker ownership.
- The MCP decision with supporting measurements, or the reason no reliable measurement is available.
- Release/version status and whether installed plugins were updated.

Primary work is complete when the ordinary lifecycle uses documented helpers without custom
bookkeeping code, the measured interaction overhead is reduced, the invariants hold on both
supported state versions and host launchers, and the skill teaches the implemented path.
