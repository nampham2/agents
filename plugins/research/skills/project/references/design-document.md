# Design document

`artifacts/architecture.md` is the living technical design of a project that writes code. The
first task of such a project writes it, after reading the code and sources the plan relies on; every
later code task keeps it current. A Goldfish round ([goldfish-review.md](goldfish-review.md)) must
review it before the first code task starts; later code tasks edit it without a new round.
`edit <project-dir> architecture` is guarded by the content token, `read <project-dir>
architecture` reads it, and the user may review it at any time. A legacy project keeps its
agreement in a root `architecture.md`; use the id `planning` there.

Write for a reader who has not seen the project. Each section is present, or its heading is
followed by `Not applicable: <reason>`. Validation warns, and never blocks, for a project created
after the split when the file is missing, lacks a section its profiles require, or lacks a status
row for a `DONE` code task.

## Header

Title, project id, `Template: v1`, `Profiles:` (below), links to `spec.md` and `planning.md`, and
`Reconciled through: Tn`, the last task whose changes the document reflects.

## Sections

| # | Section | Content |
| --- | --- | --- |
| 1 | Purpose and quality attributes | Goals, non-goals, measurable targets: latency, throughput, availability, cost, security, compatibility |
| 2 | Context and containers | C4 level 1 and 2 diagrams in Mermaid; external systems and trust boundaries |
| 3 | Modules and code layout | Per module: responsibility, public interface, state owned, dependencies, reuse versus change, code and test paths; directory tree |
| 4 | Data model and contracts | Schemas, invariants, versioning and compatibility, CLI or API contracts, error taxonomy |
| 5 | Key flows | Sequence diagrams for success and failure; state diagrams for lifecycles |
| 6 | Algorithms | Per algorithm: problem, inputs and outputs, invariants, pseudocode, time and space complexity, correctness argument, edge cases, parameters, rejected alternatives |
| 7 | Concurrency and distribution | Processing model; partitioning key; ordering and delivery guarantees; idempotency; consistency; locking; backpressure; retry and timeout policy; crash recovery; scale limits. Parallel algorithms: decomposition, work distribution, synchronization, speedup limits, determinism |
| 8 | Failure modes and recovery | Table: failure, detection, impact, mitigation, owner, test |
| 9 | Security, privacy and limits | Access control, secrets, data handling, resource limits |
| 10 | Evaluation plan | Correctness tests by level; benchmarks (workload, metric, baseline, target, method, environment); for data or ML work datasets, metrics, baselines, ablations, thresholds, statistical method; each success criterion traced to a check |
| 11 | Operations and rollout | Deployment, configuration, observability, migration, rollback, runbook pointers |
| 12 | Decisions | Context, decision, consequences and status per entry; links to `planning.md` decisions |
| 13 | Implementation status | One row per task id: module, status, deviation from the design; known gaps |

## Profiles

Profiles choose which sections go deep, so a small change is not buried. `core` always: sections
1, 2, 3, 5, 8, 10, 12 and 13. `algorithmic` adds 6, `concurrent-distributed` adds 7, `service` adds
9 and 11, and `data-ml` deepens 10 with datasets and ablations. `planning.md` names the profiles;
the design task confirms them on the `Profiles:` line, comma separated.

## Maintenance

- Lead with diagrams; names match real code paths and modules.
- Cite sources with `file:line`, read from numbered output.
- Give no time estimates.
- Remove stale text rather than add a contradiction.
- A code task that departs from the design edits the affected section, adds a section 12 entry
  and sets its section 13 row, and moves `Reconciled through`.
- A blocking design question reopens planning through [execution-changes.md](execution-changes.md);
  do not settle it inside the document.
