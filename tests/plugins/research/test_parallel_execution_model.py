"""A small stateful reference model of the parallel-execution design (§21.2 of the reference).

Two coordinators, two tasks, one shared write path, one checker, one launch capability. Canonical
state and external execution are modelled **separately**: `Store` holds the durable records, the
registry and the canonical task state, `World` holds the modelled processes and the modelled bytes
they write, and no guard in this file reads a process to decide anything — guards read records,
invariants read processes. That separation is the whole point. Revision 4's suite derived "the worker
is running" from "the record says running", so every question it could ask had been answered by
construction.

**One entry point.** `perform` is the only way a test or recovery applies an operation: it runs that
operation's guard, refuses without touching the store if the guard refuses, and otherwise applies the
durable effects one at a time, checking the invariants after each. `force` is the deliberately
separate negative-test API that bypasses the guard, and nothing but `ForcedViolationTests` and the
named regression cases that document a bypass may call it. Revision 5's model checked guards in one
place and applied effects in another, so recovery could commit work no guard had approved.

Recovery is a function from a world to a sequence of operations (`recover`) whose guards and durable
effects `perform` then runs, so recovery is exercised rather than asserted, and a recovery step whose
precondition has gone stale is refused rather than applied. It reads the world and not only the store
because §8.1's O3 completion re-reads the baseline digests, which are bytes. Crashes are injected
between durable effects, at every prefix of every operation's sequence.

The five invariants are stated externally, over the modelled world rather than over a label:

* **I1** no two executors hold conflicting claims — read off the registry of active grants, which is
  a separate structure from the journal because §8.1's O16 removes the grant in a *second* durable
  step after publishing the release.
* **I2** no claim is released while any modelled process can still write, including one whose
  coordinator is dead and whose launch capability is unconsumed, and including a worker that has
  published a result on a host whose adapter cannot seal it.
* **I3** the bytes and the definition a canonical mutation was computed over still hold **at the
  commit effect**, not merely when the intent was published. Checking it at the intent was revision
  5's defect: everything the mutation is exposed to happens between those two effects.
* **I4** a task is `DONE` only if its `evidence` holds a receipt that recomputes from a published
  intent — checked both against the model's own receipt derivation and by building the real candidate
  and dry-running it through the real `check_candidate`.
* **I5** no projection reads a malformed, unexplainable or null record as absence: an unreadable
  record stops the reader instead of counting as nothing.

Every invariant is shown reachable-failure by `ForcedViolationTests`, which bypasses the guards and
requires the violation to appear. An invariant no scenario can break is not being checked.

**Passing this model does not establish that the protocol is correct**, and its scope is narrower
than §8's operation table. Modelled: O1, O2, O3, O4, O5, O6, O7, O8, O9, O10, O11, O12, O13, O16, O17
and O18. O15 (`stop`) is modelled only as far as its first two durable effects — the durable stop
request and the `operator_stop` hold — because steps 3 to 5 need an adapter that can be asked to
terminate. Not modelled: O14 (`retry`, whose durable shape is O12's over a `BLOCKED` task), and O19
and O20 (ownership acquisition and relinquishment, which need more than one live coordinator process
to be worth modelling). There is no executor, no filesystem and no adapter behind any of it; the
world has one write path and one checker; and `check_candidate` validates a v3 candidate's schema and
transitions — not the v4 `execution` block, not a receipt's meaning, and not any output declaration —
so the receipt half of I4 is this file's own assertion.

Eight of review 04's eighteen findings — R4-05, R4-09, R4-11, R4-13, R4-15, R4-16, R4-17 and R4-18 —
are not traces of this state machine and are not modelled here: they are answered in the reference by
§6 and §11.1, by refusal codes in §4, by this replacement itself, by §17.1, and by the documentation
checker and §21.3. Review 05's R5-07 is answered by this file's five repaired probes, each of which
has a named regression case in `ReviewFiveRegressionTests`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from workspace_lib import allocate_project, check_candidate

# --------------------------------------------------------------------------------------------------
# The modelled world
# --------------------------------------------------------------------------------------------------

SUBJECT = "out.txt"
OTHER_SUBJECT = "other.txt"
TASK = "produce-output"
OTHER_TASK = "produce-other"
COORDINATOR_A = "run-a"
COORDINATOR_B = "run-b"
ATTEMPT_A = "att-a"
ATTEMPT_B = "att-b"
CHECK = "assert-nonempty"

DEFINITION: "Dict[str, Dict[str, str]]" = {
    TASK: {"subject": SUBJECT, "check": CHECK, "instruction": "write a non-empty out.txt"},
    OTHER_TASK: {"subject": OTHER_SUBJECT, "check": CHECK, "instruction": "write other.txt"},
}

# §12.2: the three proofs of death. Staleness and a different host are triggers, never proofs.
DEATH_PROOFS = frozenset({"host_rebooted", "pid_gone", "operator_attestation"})

# A sentinel distinct from a stored JSON null, which is what revision 5's `records.get(path)` could
# not distinguish from a missing file. That collapse is R5-07's first probe.
MISSING = object()

# The fields each record kind must carry for a projection to read it at all (§6.3, §7.4). The model
# spells §7.4's `record_kind` as `kind`, so the `scope` record's own `kind` field (`worker` or `check`)
# is `scope_kind` here; every other field name is the reference's.
RECORD_FIELDS: "Dict[str, frozenset]" = {
    "reservation": frozenset({"kind", "attempt", "coordinator", "task", "claims", "definition_hash"}),
    "scope": frozenset({"kind", "attempt", "coordinator", "scope_id", "scope_kind", "claims"}),
    "prepared": frozenset({"kind", "attempt", "coordinator", "definition_hash", "baseline",
                           "instruction_digest"}),
    "launch": frozenset({"kind", "attempt", "coordinator", "start_outcome", "capability_id"}),
    "result": frozenset({"kind", "attempt", "coordinator", "outcome", "produced", "baseline_matched",
                         "captures", "summary"}),
    "capture": frozenset({"kind", "attempt", "coordinator", "check_id", "sequence", "subjects",
                          "exit", "code"}),
    "assessment": frozenset({"kind", "attempt", "coordinator", "check_id", "sequence", "capture_id",
                             "capture_body_digest", "assessor", "adequate"}),
    "acceptance": frozenset({"kind", "attempt", "coordinator", "task", "intent", "receipt_id",
                             "definition_hash", "accepted_snapshot", "qualifying_captures"}),
    "fence": frozenset({"kind", "attempt", "coordinator", "receipt_id", "sequence",
                        "expected_revision"}),
    "commit-observed": frozenset({"kind", "attempt", "coordinator", "receipt_id", "fence_sequence",
                                  "committed_revision", "evidence_index", "committed_status"}),
    "release": frozenset({"kind", "attempt", "coordinator", "receipt", "sealed_scopes",
                          "grant_removed"}),
    "sealed": frozenset({"kind", "attempt", "coordinator", "scope_id", "exit", "tree_exited",
                         "resumable"}),
    "classification": frozenset({"kind", "attempt", "coordinator", "task", "class"}),
    "hold": frozenset({"kind", "attempt", "coordinator", "task", "cause_id", "cause_class"}),
    "resolution": frozenset({"kind", "attempt", "coordinator", "task", "cause_id", "resolution",
                             "observed_revision"}),
    # An attempt fence also carries `attempt`; the store-root one does not, so only the fields
    # both scopes carry are required (§7.4's two envelopes).
    "generation-fence": frozenset({"kind", "coordinator", "generation", "taken_over_from", "scope",
                                   "records"}),
    # §7.4's two envelopes: the project-level kinds carry no `attempt`, and a projection of an attempt
    # never reads them for one.
    "owner": frozenset({"kind", "coordinator", "generation", "host_id", "boot_id", "pid"}),
    "stop-request": frozenset({"kind", "coordinator", "sequence", "requested_by", "reason"}),
    "stop-clearance": frozenset({"kind", "coordinator", "sequence", "clears", "cleared_by"}),
}

# §14.1's fourteen cause classes, reduced to the ones this model raises.
CAUSE_CLASSES = frozenset({"adapter_error", "definition_changed", "stale_evidence", "operator_stop",
                           "human_review"})


def digest(value: object) -> str:
    """§7.3's canonical serialization, reduced to what the model needs: sorted keys, no whitespace."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_for(
    accepted_snapshot: "Dict[str, Optional[str]]",
    attempt: str,
    definition_hash: str,
    intent: str,
    qualifying_captures: "List[str]",
) -> str:
    """§7.4: `rcp-` plus 32 hex over the five-key object. `receipt_id` itself is not one of the keys,
    which is what makes the mutation's identity a fixed point rather than a self-reference."""
    body = {
        "accepted_snapshot": accepted_snapshot,
        "attempt_id": attempt,
        "definition_hash": definition_hash,
        "intent": intent,
        "qualifying_captures": qualifying_captures,
    }
    return "rcp-" + digest(body)[:32]


def scope_path(attempt: str, scope_id: str) -> str:
    """§7.3's filename encoding: `:` is `%3A` and `#` is `%23`."""
    encoded = scope_id.replace(":", "%3A").replace("#", "%23")
    return f"attempts/{attempt}/scopes/{encoded}.json"


def sealed_path(attempt: str, scope_id: str) -> str:
    encoded = scope_id.replace(":", "%3A").replace("#", "%23")
    return f"attempts/{attempt}/sealed/{encoded}.json"


def mutation_dir(attempt: str, receipt: str) -> str:
    return f"attempts/{attempt}/mutations/{receipt}"


@dataclass
class Adapter:
    """§17.1: what a host's executor can be evidenced to do, not what its documentation says."""

    name: str
    seals: bool          # a finished worker can be attested gone
    resumable: bool      # a finished worker can be restarted, so "finished" is not "sealed"


SEALING = Adapter("sealing", seals=True, resumable=False)
RESUMABLE = Adapter("resumable", seals=False, resumable=True)


# §7.3's `content_digest` excludes the fields the *writer* stamps rather than derives. The model
# spells §7.4's `ownership_generation` as `written_in` and its `coordinator_run` as `coordinator`;
# `writer` and `written_at` have no model counterpart because there is one writer and no clock here.
WRITER_STAMPED = frozenset({"written_in", "coordinator"})


def content_of(record: object) -> object:
    """§7.3's `content_digest` input: the record without the fields its writer stamps.

    A comparison function, not a field. `body_digest` covers the whole record because it is tamper
    evidence; this covers what two publications of the same record must agree about. `coordinator` is
    in the excluded set for the same reason the generation is: a successor completing a predecessor's
    operation re-derives the identical record under its own run id, and §8.2 requires that completion
    to read as `identical`.
    """
    if not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k not in WRITER_STAMPED}


@dataclass
class Store:
    """The durable side. Three structures, because the design has three and conflates none of them.

    `records` is the publish-if-absent journal. `registry` is the grant registry of §10.4, a mutable
    table under its own lock: a grant is inserted by O2, its `capability` field is set by O4, and it is
    removed by O16's *second* durable effect. `canonical` is `project.json`'s task state. Revision 5's
    model derived the registry from the journal — a `release` record meant the grant was gone — which
    erased the one prefix where the journal is ahead of the registry (§8.2's O16 row) and made I1
    unfalsifiable there.
    """

    records: "Dict[str, Any]" = field(default_factory=dict)
    registry: "Dict[str, Dict[str, Any]]" = field(default_factory=dict)
    canonical: "Dict[str, Dict[str, Any]]" = field(default_factory=dict)
    ownership: "Dict[str, Any]" = field(
        default_factory=lambda: {"coordinator_run": COORDINATOR_A, "generation": 0}
    )
    evidence_log: "List[str]" = field(default_factory=list)
    revision: int = 0
    conflicts: "List[str]" = field(default_factory=list)

    def publish(self, path: str, record: "Dict[str, Any]") -> str:
        """§7.2: never clobber. Identical *content* is success; different content is a recorded event.

        The writer stamps `written_in` (standing in for §7.4's `ownership_generation`) rather than the
        caller, because the generation is a property of whoever is publishing, not of the operation
        being completed. That is what made the comparison a question: after a takeover, a successor
        completing a predecessor's operation re-derives the same record under a new generation, so byte
        equality would report `conflict` for exactly the completions §8.2 requires. Revision 6 said
        "equal bytes" in §7.2 and promised "`created` or `identical`" in §8.2, and those cannot both
        hold while the envelope carries a generation and a `written_at` — hence §7.3's `content_digest`,
        which this method models by comparing records with the writer-stamped fields removed.
        """
        stamped = copy.deepcopy(record)
        stamped["written_in"] = int(self.ownership["generation"])
        existing = self.records.get(path, MISSING)
        if existing is MISSING:
            self.records[path] = stamped
            return "published"
        if content_of(existing) == content_of(stamped):
            return "identical"
        self.conflicts.append(path)
        return "conflict"

    def has(self, path: str) -> bool:
        return path in self.records

    def of_kind(self, kind: str) -> "List[Dict[str, Any]]":
        return [r for r in self.records.values() if isinstance(r, dict) and r.get("kind") == kind]


@dataclass
class World:
    """Everything the store cannot see: the processes, the bytes, and the host's adapter."""

    adapter: Adapter = field(default_factory=lambda: SEALING)
    store: Store = field(default_factory=Store)
    armed: "Set[str]" = field(default_factory=set)     # capability issued, spawn not yet fired
    running: "Set[str]" = field(default_factory=set)   # a process exists and can write
    content: "Dict[str, str]" = field(default_factory=dict)
    definition: "Dict[str, Dict[str, str]]" = field(default_factory=lambda: copy.deepcopy(DEFINITION))
    read_after_indeterminate: "List[str]" = field(default_factory=list)


def can_write(world: World, attempt: str) -> bool:
    """A launch that has been authorized but not yet fired can still write: that is R4-01."""
    return attempt in world.armed or attempt in world.running


# --------------------------------------------------------------------------------------------------
# Facts derived from records and the registry (§6.1) — never from a process
# --------------------------------------------------------------------------------------------------


def capability(store: Store, attempt: str) -> str:
    """`none → issued → consumed`, read off the registry field O4 sets — not off the result record.

    §8.1's O4 issues the capability at step 5, calls the adapter at step 6, publishes `launch` at step
    7 and consumes at step 8. Revision 5's model consumed it when `result.json` appeared, which is a
    worker-written record several operations later: between those points, an attempt whose worker had
    exited read as still in flight, and an attempt whose worker had published a result read as
    consumed even though the capability had never been marked used. R5-07 probed the second half.
    """
    row = store.registry.get(attempt)
    if row is None:
        return "none"
    return str(row.get("capability", "none"))


def active_grants(store: Store) -> "Dict[str, Dict[str, Any]]":
    """The registry, and only the registry. A published `release` does not remove a grant (§8.1 O16)."""
    return dict(store.registry)


def declared_scopes(store: Store, attempt: str) -> "Set[str]":
    """§6.1: read from the `scope` records, so a scope that was never declared is never expected."""
    return {
        str(r["scope_id"]) for r in store.of_kind("scope") if r.get("attempt") == attempt
    }


def sealed_scopes(store: Store, attempt: str) -> "Set[str]":
    return {
        str(r["scope_id"]) for r in store.of_kind("sealed") if r.get("attempt") == attempt
    }


def open_scopes(store: Store, attempt: str) -> "Set[str]":
    return declared_scopes(store, attempt) - sealed_scopes(store, attempt)


def open_causes(store: Store) -> "Set[str]":
    """§14.2: the dispatch gate is a **project-level** fact, not a per-task one.

    An unresolved failure holds the whole project, because the coordinator cannot tell whether the
    cause is confined to the task that surfaced it. Revision 5's model filtered by task, so R5-07's
    fifth probe — an unresolved failure on task A, then a reservation of unrelated task B — walked
    straight through a gate the reference says is closed.
    """
    resolved = {r["cause_id"] for r in store.of_kind("resolution")}
    return {
        r["cause_id"]
        for r in store.of_kind("hold")
        if r["cause_class"] != "operator_stop" and r["cause_id"] not in resolved
    }


def stop_requested(store: Store) -> bool:
    """§10.1's durable stop gate: a request with no clearance of its sequence."""
    cleared = {int(r["clears"]) for r in store.of_kind("stop-clearance")}
    return any(int(r["sequence"]) not in cleared for r in store.of_kind("stop-request"))


def explained_generations(store: Store) -> "Set[int]":
    return {int(store.ownership["generation"])} | {
        int(r["generation"]) for r in store.of_kind("generation-fence")
    }


def fenced_paths(store: Store, generation: int) -> "Set[str]":
    paths: "Set[str]" = set()
    for record in store.of_kind("generation-fence"):
        if int(record["generation"]) == generation:
            paths |= set(record["records"])
    return paths


def projection(store: Store, path: str) -> str:
    """§6.3 and I5: `present`, `absent`, or `indeterminate`. A record that exists is never absent.

    `MISSING` rather than `None` is the absence test, because a stored JSON null is a record that
    exists and cannot be read — R5-07's first probe, which revision 5's model reported as `absent`.
    """
    record = store.records.get(path, MISSING)
    if record is MISSING:
        return "absent"
    if not isinstance(record, dict):
        return "indeterminate"
    required = RECORD_FIELDS.get(record.get("kind"))
    if required is None or not required <= set(record):
        return "indeterminate"
    generation = record.get("generation")
    if generation is not None and int(generation) not in explained_generations(store):
        return "indeterminate"
    current = int(store.ownership["generation"])
    written = record.get("written_in")
    if written is not None and int(written) != current:
        # §6.3's second clause: an earlier generation's record is explainable only if this
        # generation's fence lists its own store-relative path.
        if path not in fenced_paths(store, current):
            return "indeterminate"
    return "present"


def indeterminate_records(store: Store) -> "List[str]":
    return sorted(p for p in store.records if projection(store, p) == "indeterminate")


def snapshot_of(world: World, subjects: "List[str]") -> "Dict[str, Optional[str]]":
    return {s: (digest(world.content[s]) if s in world.content else None) for s in subjects}


# --------------------------------------------------------------------------------------------------
# Durable effects and the operations that order them (§8.1, §8.2)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Publish:
    path: str
    record: "Dict[str, Any]"


@dataclass(frozen=True)
class Registry:
    """One durable step under the registry lock (§10.4): insert a grant, set its capability, remove it.

    `issue` also arms the modelled world, because the registry field flipping to `issued` is exactly
    what authorizes a launch: from that effect onward a spawn may fire at any time, including after
    the coordinator that issued it has died.
    """

    action: str      # insert | issue | consume | remove
    attempt: str
    row: "Optional[Dict[str, Any]]" = None


@dataclass(frozen=True)
class Start:
    """The adapter's `start` call. Not durable, and deliberately in the effect list anyway: §8.2 has a
    prefix "after 6, before 7" whose whole content is that this call happened and nothing recorded it."""

    attempt: str


@dataclass(frozen=True)
class AppendEvidence:
    """O12 step 1. Deduplicated on the receipt, so an interrupted commit appends once (§8.3)."""

    receipt: str


@dataclass(frozen=True)
class CommitCanonical:
    """The canonical mutation, carrying everything I3 needs to check it **here** rather than at the
    intent: the bytes and the definition the receipt was computed over."""

    task: str
    status: str
    receipt: str
    accepted_snapshot: "Dict[str, Optional[str]]"
    definition_hash: str
    intent: str


@dataclass(frozen=True)
class CommitOwnership:
    coordinator: str
    generation: int


Effect = Union[Publish, Registry, Start, AppendEvidence, CommitCanonical, CommitOwnership]


@dataclass
class Operation:
    """An operation is its id, its ordered durable effects, and its guard. The guard is part of the
    operation rather than a separate table, so `perform` cannot apply effects no guard approved."""

    name: str
    code: str
    effects: "List[Effect]"
    guard: "Optional[Callable[[World], Optional[str]]]" = None
    # §12.4: O17 is the one operation whose precondition tolerates an indeterminate store, because
    # publishing the generation fence is what *ends* the indeterminacy. Everything else is refused.
    tolerates_indeterminate: bool = False


def apply_effect(world: World, effect: Effect) -> None:
    store = world.store
    if isinstance(effect, Publish):
        store.publish(effect.path, effect.record)
    elif isinstance(effect, Registry):
        if effect.action == "insert":
            store.registry.setdefault(effect.attempt, dict(effect.row or {}, capability="none"))
        elif effect.action == "issue":
            store.registry[effect.attempt]["capability"] = "issued"
            world.armed.add(effect.attempt)
        elif effect.action == "consume":
            store.registry[effect.attempt]["capability"] = "consumed"
        else:
            store.registry.pop(effect.attempt, None)
    elif isinstance(effect, Start):
        # The capability stops being a *pending* write authority the moment the spawn fires and starts
        # being an actual process. Leaving the attempt armed as well would make `can_write` true forever,
        # so I2 could never be satisfied and O16 could never be modelled at all.
        world.armed.discard(effect.attempt)
        world.running.add(effect.attempt)
    elif isinstance(effect, AppendEvidence):
        if effect.receipt not in store.evidence_log:
            store.evidence_log.append(effect.receipt)
    elif isinstance(effect, CommitOwnership):
        store.ownership = {"coordinator_run": effect.coordinator, "generation": effect.generation}
        store.revision += 1
    else:
        entry = store.canonical.setdefault(effect.task, {"status": "TODO", "evidence": []})
        entry["status"] = effect.status
        if effect.status != "RUNNING" and effect.receipt not in entry["evidence"]:
            entry["evidence"].append(effect.receipt)
        store.revision += 1


# --------------------------------------------------------------------------------------------------
# Guards: refusal codes over facts (§4). Nothing here reads a process.
# --------------------------------------------------------------------------------------------------


def refuse_common(world: World) -> "Optional[str]":
    if indeterminate_records(world.store):
        return "R-INDETERMINATE"
    return None


def refuse_reserve(world: World, task: str, claim: str) -> "Optional[str]":
    if not world.adapter.seals:
        return "R-DETACHED-CHILD"
    if open_causes(world.store):
        return "R-GATE-CLOSED"
    if stop_requested(world.store):
        return "R-STOP-REQUESTED"
    del task
    for row in active_grants(world.store).values():
        if claim in row["claims"]:
            return "R-CLAIM-CONFLICT"
    return None


def refuse_grant(world: World, attempt: str, claim: str) -> "Optional[str]":
    if not world.store.has(f"attempts/{attempt}/reservation.json"):
        return "R-NO-RESERVATION"
    for other, row in active_grants(world.store).items():
        if other != attempt and claim in row["claims"]:
            return "R-CLAIM-CONFLICT"
    return None


def refuse_prepare(world: World, attempt: str) -> "Optional[str]":
    if attempt not in active_grants(world.store):
        return "R-NO-GRANT"
    return None


def refuse_dispatch(world: World, attempt: str, task: str) -> "Optional[str]":
    if not world.store.has(f"attempts/{attempt}/prepared.json"):
        return "R-NOT-PREPARED"
    if attempt not in active_grants(world.store):
        return "R-NO-GRANT"
    if capability(world.store, attempt) != "none":
        return "R-CAPABILITY-ISSUED"
    if open_causes(world.store):
        return "R-GATE-CLOSED"
    if stop_requested(world.store):
        return "R-STOP-REQUESTED"
    if world.store.canonical.get(task, {}).get("status", "TODO") != "TODO":
        return "R-NOT-TODO"
    return None


def refuse_run_check(world: World, attempt: str) -> "Optional[str]":
    if not world.store.has(f"attempts/{attempt}/prepared.json"):
        return "R-NOT-PREPARED"
    return None


def revalidate(
    world: World, attempt: str, task: str, receipt: str,
) -> "Optional[str]":
    """The precondition O11 and O12 share, and the one recovery must re-run before it commits.

    R5-07's third probe: publish an acceptance intent, change the output bytes, then recover. Revision
    5's `recover` emitted a bare `CommitCanonical` with no guard at all, so the model committed `DONE`
    over evidence that had already gone stale and reported nothing. The intent is content-addressed,
    so re-deriving the receipt from the *current* world is the whole check: a changed definition or a
    changed byte changes the receipt, and a receipt that no longer matches the one in the intent means
    the mutation may not be submitted.
    """
    intent = world.store.records.get(f"{mutation_dir(attempt, receipt)}/intent.json", MISSING)
    if intent is MISSING or not isinstance(intent, dict):
        return "R-NO-INTENT"
    if digest(world.definition) != intent["definition_hash"]:
        return "definition_changed"
    subjects = sorted(intent["accepted_snapshot"])
    if intent["accepted_snapshot"] != snapshot_of(world, subjects):
        return "stale_evidence"
    recomputed = receipt_for(
        intent["accepted_snapshot"], attempt, digest(world.definition),
        intent["intent"], list(intent["qualifying_captures"]),
    )
    if recomputed != receipt:
        return "stale_evidence"
    del task
    return None


def refuse_accept(world: World, attempt: str, task: str) -> "Optional[str]":
    if open_scopes(world.store, attempt):
        return "R-SCOPE-OPEN"
    if open_causes(world.store):
        return "R-GATE-CLOSED"
    if not world.store.has(f"attempts/{attempt}/classification.json"):
        return "R-NOT-CLASSIFIED"
    capture_path = f"attempts/{attempt}/captures/{CHECK}/0001-cap-{attempt}.json"
    capture = world.store.records.get(capture_path, MISSING)
    if capture is MISSING:
        return "R-NO-CAPTURE"
    assessment = world.store.records.get(f"attempts/{attempt}/assessments/{CHECK}/0001.json", MISSING)
    if assessment is MISSING or not isinstance(assessment, dict) or not assessment["adequate"]:
        return "R-CHECK-INADEQUATE"
    if digest(world.definition) != world.store.records[
        f"attempts/{attempt}/prepared.json"
    ]["definition_hash"]:
        return "definition_changed"
    for subject in capture["subjects"]:
        if snapshot_of(world, [subject["path"]])[subject["path"]] != subject["after"]:
            return "stale_evidence"
    del task
    return None


def refuse_release(world: World, attempt: str) -> "Optional[str]":
    """I2's guard, and O16's precondition. A seal is required whenever a capability was ever issued.

    R5-07's second probe: a worker that has published its result but has no seal. Revision 5 gated that
    on `world.adapter.resumable`, which is a property of the *host*, so on a sealing adapter the
    release went through on the strength of a worker-written record. The protocol's rule is not
    conditional: §8.1's O16 requires `sealed_scopes` to equal `declared_scopes`, and §17.3 says only an
    adapter attestation establishes that. `none` is the one exception, and it is O18's path, not O16's.
    """
    if attempt not in active_grants(world.store):
        return None
    state = capability(world.store, attempt)
    if state != "none" and open_scopes(world.store, attempt):
        return "R-LAUNCH-IN-FLIGHT"
    if state != "none" and not world.store.has(sealed_path(attempt, "worker")):
        return "R-LAUNCH-IN-FLIGHT"
    if open_causes(world.store):
        return "R-GATE-CLOSED"
    return None


def refuse_dispose(world: World, attempt: str) -> "Optional[str]":
    """O18 is the complement of O16: it exists precisely for `launch_capability` `none`."""
    if capability(world.store, attempt) != "none":
        return "R-CAPABILITY-ISSUED"
    return None


def refuse_take_over(world: World, dead: str, proof: str) -> "Optional[str]":
    """§12.2: the predecessor must be *proven* dead. Staleness and a different host are triggers.

    The proof is an argument over the predecessor's `owner` record, which is why no `death-proof` record
    kind appears here or in §7.4. Revision 5's model published one, and a record kind invented by a test
    is a protocol the reference does not describe.
    """
    if str(world.store.ownership["coordinator_run"]) != dead:
        return "R-NOT-THE-OWNER"
    if proof not in DEATH_PROOFS:
        return "R-NO-PROOF-OF-DEATH"
    return None


def refuse_withdraw(world: World, attempt: str, task: str) -> "Optional[str]":
    """O13: a withdrawal is authorized by a `resolution`, never by the coordinator changing its mind."""
    if open_scopes(world.store, attempt):
        return "R-SCOPE-OPEN"
    dispositions = {
        r["resolution"] for r in world.store.of_kind("resolution") if r.get("attempt") == attempt
    }
    if "withdraw" not in dispositions:
        return "R-NO-WITHDRAWAL"
    if world.store.canonical.get(task, {}).get("status") != "RUNNING":
        return "R-NOT-RUNNING"
    return None


def refuse_request_stop(world: World) -> "Optional[str]":
    """O15's own precondition is only that an operator asked, so the model's guard is the common one."""
    return refuse_common(world)


# --------------------------------------------------------------------------------------------------
# The operations (§8.1), each carrying its own guard
# --------------------------------------------------------------------------------------------------


def op_reserve(attempt: str, coordinator: str, task: str, claim: str, definition_hash: str) -> Operation:
    return Operation("reserve", "O1", [
        Publish(f"attempts/{attempt}/reservation.json", {
            "kind": "reservation", "attempt": attempt, "coordinator": coordinator, "task": task,
            "claims": [claim], "definition_hash": definition_hash,
        }),
    ], guard=lambda w: refuse_reserve(w, task, claim))


def op_grant(attempt: str, coordinator: str, task: str, claim: str, definition_hash: str) -> Operation:
    """O2 writes the registry and nothing else: a grant is a mutable row, not a journal record (§10.4)."""
    del coordinator
    return Operation("grant", "O2", [
        Registry("insert", attempt, {"task": task, "claims": [claim],
                                     "definition_hash": definition_hash}),
    ], guard=lambda w: refuse_grant(w, attempt, claim))


def op_prepare(attempt: str, coordinator: str, task: str, claim: str, baseline: "Optional[str]") -> Operation:
    """O3: read the baseline, declare the worker scope, publish the contract — in that order."""
    return Operation("prepare", "O3", [
        Publish(scope_path(attempt, "worker"), {
            "kind": "scope", "attempt": attempt, "coordinator": coordinator, "scope_id": "worker",
            "scope_kind": "worker", "claims": [claim],
        }),
        Publish(f"attempts/{attempt}/prepared.json", {
            "kind": "prepared", "attempt": attempt, "coordinator": coordinator,
            "definition_hash": digest(DEFINITION), "baseline": {claim: baseline},
            "instruction_digest": digest(DEFINITION[task]["instruction"]),
        }),
    ], guard=lambda w: refuse_prepare(w, attempt))


def op_dispatch(attempt: str, coordinator: str, task: str) -> Operation:
    """O4's eight durable steps. R4-01 lives between 5 and 6, and §8.2's ambiguous prefix between
    6 and 7: the capability is issued before the adapter is called, and the adapter is called before
    anything records that it was."""
    receipt = receipt_for({}, attempt, digest(DEFINITION), "RUNNING", [])
    directory = mutation_dir(attempt, receipt)
    return Operation("dispatch", "O4", [
        Publish(f"{directory}/intent.json", {
            "kind": "acceptance", "attempt": attempt, "coordinator": coordinator, "task": task,
            "intent": "RUNNING", "receipt_id": receipt, "definition_hash": digest(DEFINITION),
            "accepted_snapshot": {}, "qualifying_captures": [],
        }),
        Publish(f"{directory}/fences/0001.json", {
            "kind": "fence", "attempt": attempt, "coordinator": coordinator, "receipt_id": receipt,
            "sequence": 1, "expected_revision": 0,
        }),
        CommitCanonical(task, "RUNNING", receipt, {}, digest(DEFINITION), "RUNNING"),
        Publish(f"{directory}/ack.json", {
            "kind": "commit-observed", "attempt": attempt, "coordinator": coordinator,
            "receipt_id": receipt, "fence_sequence": 1, "committed_revision": 1,
            "evidence_index": 0, "committed_status": "RUNNING",
        }),
        Registry("issue", attempt),
        Start(attempt),
        Publish(f"attempts/{attempt}/launch.json", {
            "kind": "launch", "attempt": attempt, "coordinator": coordinator,
            "start_outcome": "started", "capability_id": f"cap-{attempt}",
        }),
        Registry("consume", attempt),
    ], guard=lambda w: refuse_dispatch(w, attempt, task))


def op_record_result(
    attempt: str, coordinator: str, claim: str, produced: "Optional[str]",
    outcome: str = "success",
) -> Operation:
    return Operation("record-result", "O5", [
        Publish(f"attempts/{attempt}/result.json", {
            "kind": "result", "attempt": attempt, "coordinator": coordinator, "outcome": outcome,
            "produced": {claim: produced}, "baseline_matched": True,
            "captures": [{"check_id": CHECK, "sequence": 1, "capture_id": f"cap-{attempt}"}],
            "summary": "the modelled worker performed the instruction and ran the check",
        }),
    ], guard=lambda w: None if capability(w.store, attempt) != "none" else "R-NO-CAPABILITY")


def op_run_check(attempt: str, coordinator: str, claim: str, after: "Optional[str]") -> Operation:
    """O10's five durable steps. The scope record is first, before any process runs (§6.1)."""
    scope_id = f"check:{CHECK}#0001"
    body = {"path": claim, "after": after}
    return Operation("run-check", "O10", [
        Publish(scope_path(attempt, scope_id), {
            "kind": "scope", "attempt": attempt, "coordinator": coordinator, "scope_id": scope_id,
            "scope_kind": "check", "claims": [claim],
        }),
        Publish(f"attempts/{attempt}/captures/{CHECK}/0001-cap-{attempt}.json", {
            "kind": "capture", "attempt": attempt, "coordinator": coordinator, "check_id": CHECK,
            "sequence": 1, "subjects": [body], "exit": "exited", "code": 0,
        }),
        Publish(f"attempts/{attempt}/assessments/{CHECK}/0001.json", {
            "kind": "assessment", "attempt": attempt, "coordinator": coordinator, "check_id": CHECK,
            "sequence": 1, "capture_id": f"cap-{attempt}",
            "capture_body_digest": digest([body]),
            "assessor": {"kind": "exit_status", "identity": "the check's own exit status"},
            "adequate": True,
        }),
        Publish(sealed_path(attempt, scope_id), {
            "kind": "sealed", "attempt": attempt, "coordinator": coordinator, "scope_id": scope_id,
            "exit": "exited", "tree_exited": True, "resumable": False,
        }),
    ], guard=lambda w: refuse_run_check(w, attempt))


def op_seal(attempt: str, coordinator: str, scope_id: str, exit_variant: str = "exited") -> Operation:
    return Operation("seal", "O6", [
        Publish(sealed_path(attempt, scope_id), {
            "kind": "sealed", "attempt": attempt, "coordinator": coordinator, "scope_id": scope_id,
            "exit": exit_variant, "tree_exited": True, "resumable": False,
        }),
    ], guard=lambda w: None if w.adapter.seals else "R-DETACHED-CHILD")


def op_classify(attempt: str, coordinator: str, task: str, klass: str = "success") -> Operation:
    return Operation("classify", "O7", [
        Publish(f"attempts/{attempt}/classification.json", {
            "kind": "classification", "attempt": attempt, "coordinator": coordinator, "task": task,
            "class": klass,
        }),
    ], guard=lambda w: None if "worker" in sealed_scopes(w.store, attempt) else "R-SCOPE-OPEN")


def op_hold(attempt: str, coordinator: str, task: str, cause_id: str, cause_class: str) -> Operation:
    return Operation("hold", "O8", [
        Publish(f"attempts/{attempt}/holds/{cause_id}.json", {
            "kind": "hold", "attempt": attempt, "coordinator": coordinator, "task": task,
            "cause_id": cause_id, "cause_class": cause_class,
        }),
    ], guard=lambda w: None if cause_class in CAUSE_CLASSES else "R-UNKNOWN-CAUSE")


def op_resolve(
    attempt: str, coordinator: str, task: str, cause_id: str, disposition: str = "accept",
) -> Operation:
    return Operation("resolve", "O9", [
        Publish(f"attempts/{attempt}/resolutions/{cause_id}.json", {
            "kind": "resolution", "attempt": attempt, "coordinator": coordinator, "task": task,
            "cause_id": cause_id, "resolution": disposition, "observed_revision": 0,
        }),
    ], guard=lambda w: None if cause_id in open_causes(w.store) else "R-NO-OPEN-CAUSE")


def accept_receipt(world: World, attempt: str, claim: str) -> str:
    return receipt_for(
        snapshot_of(world, [claim]), attempt, digest(world.definition), "DONE",
        [f"{CHECK}#0001"],
    )


def op_accept(attempt: str, coordinator: str, task: str, claim: str, receipt: str,
              snapshot: "Dict[str, Optional[str]]", definition_hash: str) -> Operation:
    """O11 publishes the intent and nothing else. The commit is O12's, and I3 is checked there."""
    del claim
    return Operation("accept", "O11", [
        Publish(f"{mutation_dir(attempt, receipt)}/intent.json", {
            "kind": "acceptance", "attempt": attempt, "coordinator": coordinator, "task": task,
            "intent": "DONE", "receipt_id": receipt, "definition_hash": definition_hash,
            "accepted_snapshot": snapshot, "qualifying_captures": [f"{CHECK}#0001"],
        }),
    ], guard=lambda w: refuse_accept(w, attempt, task))


def op_commit_acceptance(attempt: str, coordinator: str, task: str, receipt: str,
                         snapshot: "Dict[str, Optional[str]]", definition_hash: str,
                         status: str = "DONE", fence: int = 1) -> Operation:
    directory = mutation_dir(attempt, receipt)
    return Operation("commit-acceptance", "O12", [
        AppendEvidence(receipt),
        Publish(f"{directory}/fences/{fence:04d}.json", {
            "kind": "fence", "attempt": attempt, "coordinator": coordinator, "receipt_id": receipt,
            "sequence": fence, "expected_revision": 1,
        }),
        CommitCanonical(task, status, receipt, snapshot, definition_hash, status),
        Publish(f"{directory}/ack.json", {
            "kind": "commit-observed", "attempt": attempt, "coordinator": coordinator,
            "receipt_id": receipt, "fence_sequence": fence, "committed_revision": 2,
            "evidence_index": 0, "committed_status": status,
        }),
    ], guard=lambda w: revalidate(w, attempt, task, receipt))


def op_release(attempt: str, coordinator: str, receipt: "Optional[str]") -> Operation:
    """O16: the journal record authorizes, the registry removal acts. Two effects, in that order."""
    return Operation("release", "O16", [
        Publish(f"attempts/{attempt}/release.json", {
            "kind": "release", "attempt": attempt, "coordinator": coordinator, "receipt": receipt,
            "sealed_scopes": sorted({"worker", f"check:{CHECK}#0001"}), "grant_removed": False,
        }),
        Registry("remove", attempt),
    ], guard=lambda w: refuse_release(w, attempt))


def op_dispose(attempt: str, coordinator: str) -> Operation:
    """O18: the release path for an attempt that never dispatched, sealed `not_started` (§11.2)."""
    return Operation("dispose", "O18", [
        Publish(sealed_path(attempt, "worker"), {
            "kind": "sealed", "attempt": attempt, "coordinator": coordinator, "scope_id": "worker",
            "exit": "not_started", "tree_exited": True, "resumable": False,
        }),
        Publish(f"attempts/{attempt}/release.json", {
            "kind": "release", "attempt": attempt, "coordinator": coordinator, "receipt": None,
            "sealed_scopes": ["worker"], "grant_removed": False,
        }),
        Registry("remove", attempt),
    ], guard=lambda w: refuse_dispose(w, attempt))


def generation_fence_effects(store: Store, coordinator: str, generation: int) -> "List[Effect]":
    """§12.4's fences, derived from the store so O17 and its completion produce the same records.

    One fence per attempt over that attempt's paths, plus one at the store root over the project-level
    paths. The root fence is the half revision 6 was missing until this model tried to recover a stop
    request across a takeover: an attempt's fence can only list paths under that attempt, so nothing
    could ever explain `control/stop-requests/0001.json` from an earlier generation.
    """
    stale = sorted(
        path for path, record in store.records.items()
        if isinstance(record, dict) and int(record.get("written_in", 0)) != generation
    )
    attempts = sorted({p.split("/")[1] for p in stale if p.startswith("attempts/")})
    effects: "List[Effect]" = []
    for attempt in attempts:
        effects.append(Publish(f"attempts/{attempt}/generation-fences/{generation}.json", {
            "kind": "generation-fence", "attempt": attempt, "coordinator": coordinator,
            "generation": generation, "taken_over_from": generation - 1, "scope": "attempt",
            "records": [p for p in stale if p.startswith(f"attempts/{attempt}/")],
        }))
    project = [p for p in stale if not p.startswith("attempts/")]
    if project:
        effects.append(Publish(f"generation-fences/{generation}.json", {
            "kind": "generation-fence", "coordinator": coordinator, "generation": generation,
            "taken_over_from": generation - 1, "scope": "project", "records": project,
        }))
    return effects


def op_take_over(store: Store, coordinator: str, dead: str, proof: str, generation: int) -> Operation:
    """O17: the commit comes first, then the generation fences (§8.1).

    Revision 5's model published a `death-proof` record the reference does not define, and published it
    *before* the commit so that a crash in between needed the proof to name its own successor. The
    reference orders it the other way round, which needs no such field: canonical ownership already
    names the new generation, so the completion for a crash after step 1 is "publish the fences", and
    §8.2's O17 row says exactly that. The proof itself is a §12.2 argument over the predecessor's
    `owner` record, not a record of its own.

    This is the one operation whose effects are applied over an indeterminate store, because until its
    fences exist every earlier-generation record is unexplainable (§12.4).
    """
    return Operation(
        "take-over", "O17",
        [CommitOwnership(coordinator, generation),
         *generation_fence_effects(store, coordinator, generation)],
        guard=lambda w: refuse_take_over(w, dead, proof),
        tolerates_indeterminate=True,
    )


def op_withdraw(attempt: str, coordinator: str, task: str, definition_hash: str,
                intent: str = "TODO") -> Operation:
    """O13: the same four-phase shape as O12, over an intent that gives the task back (§8.1).

    The definition hash is the *current* one, not the one the attempt was prepared against. A
    withdrawal is the operation a changed definition calls for (§14.1's `definition_changed`), so
    binding it to the stale hash would make the one mutation that answers a moved definition the one
    mutation I3 forbids.
    """
    receipt = receipt_for({}, attempt, definition_hash, intent, [])
    directory = mutation_dir(attempt, receipt)
    return Operation("withdraw", "O13", [
        Publish(f"{directory}/intent.json", {
            "kind": "acceptance", "attempt": attempt, "coordinator": coordinator, "task": task,
            "intent": intent, "receipt_id": receipt, "definition_hash": definition_hash,
            "accepted_snapshot": {}, "qualifying_captures": [],
        }),
        Publish(f"{directory}/fences/0001.json", {
            "kind": "fence", "attempt": attempt, "coordinator": coordinator, "receipt_id": receipt,
            "sequence": 1, "expected_revision": 1,
        }),
        CommitCanonical(task, intent, receipt, {}, definition_hash, intent),
        Publish(f"{directory}/ack.json", {
            "kind": "commit-observed", "attempt": attempt, "coordinator": coordinator,
            "receipt_id": receipt, "fence_sequence": 1, "committed_revision": 2,
            "evidence_index": 0, "committed_status": intent,
        }),
    ], guard=lambda w: refuse_withdraw(w, attempt, task))


def stop_cause_id(sequence: int) -> str:
    """Derived from the request's sequence, so a completion re-derives the same cause id (§8.2, O15)."""
    return f"stop-{sequence:04d}"


def op_request_stop(coordinator: str, sequence: int, store: Store) -> Operation:
    """O15 steps 1 and 2 only. Steps 3 to 5 call the adapter's `terminate`, which this model has no
    executor to terminate; the durable half is what §10.1's gate reads, and it is what is modelled.

    The holds are ordinary O8 holds under the attempts they stop — §7.1 has no `control/holds/`, and
    each effect is built from `op_hold` so the recovery that finds a request with a hold missing
    republishes the same record rather than a similar one.

    The covered set is read from the registry rather than passed in, because that is what a completion
    after a crash between step 1 and step 2 has to read: §8.2's O15 row says the completion continues
    at step 2, and step 2 has to arrive at the same holds without being told which they were.
    """
    cause = stop_cause_id(sequence)
    effects: "List[Effect]" = [
        Publish(f"control/stop-requests/{sequence:04d}.json", {
            "kind": "stop-request", "coordinator": coordinator, "sequence": sequence,
            "requested_by": "operator", "reason": "the modelled operator asked",
        }),
    ]
    for attempt in sorted(active_grants(store)):
        task = str(store.registry[attempt]["task"])
        effects.append(op_hold(attempt, coordinator, task, cause, "operator_stop").effects[0])
    return Operation("stop", "O15", effects, guard=lambda w: refuse_request_stop(w))


def op_clear_stop(coordinator: str, sequence: int, clears: int) -> Operation:
    """§10.1: a clearance naming the request's sequence is the only thing that lifts a stop."""
    return Operation("clear-stop", "O15", [
        Publish(f"control/clearances/{sequence:04d}.json", {
            "kind": "stop-clearance", "coordinator": coordinator, "sequence": sequence,
            "clears": clears, "cleared_by": "operator",
        }),
    ], guard=refuse_common)


# --------------------------------------------------------------------------------------------------
# One entry point: guard, then effects, then invariants (R5-07)
# --------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What an attempted operation produced: at most one refusal, or the violations it caused."""

    refusal: "Optional[str]"
    violations: "List[str]"

    @property
    def refused(self) -> bool:
        return self.refusal is not None


def perform(world: World, operation: Operation, prefix: "Optional[int]" = None) -> Outcome:
    """Apply an operation through its own guard. The only sanctioned entry point.

    Revision 5's model called guards in the tests and applied effects in `recover`, so recovery could
    commit work no precondition had approved — R5-07's third probe. Here the guard and the effects are
    reached through one function, and `recover` returns operations rather than effects precisely so it
    has to come through here too.
    """
    if not operation.tolerates_indeterminate:
        refusal = refuse_common(world)
        if refusal is not None:
            return Outcome(refusal, [])
    if operation.guard is not None:
        refusal = operation.guard(world)
        if refusal is not None:
            return Outcome(refusal, [])
    return Outcome(None, _apply(world, operation, prefix))


def force(world: World, operation: Operation, prefix: "Optional[int]" = None) -> "List[str]":
    """The negative-test API: apply the effects with no guard, and report what the invariants catch.

    Explicitly separate from `perform`, and called only by `ForcedViolationTests` and the named
    regression cases that document a bypass. An invariant no scenario can break is not being checked,
    and the way to show it can break is to break it deliberately here rather than to weaken a guard.
    """
    return _apply(world, operation, prefix)


def _apply(world: World, operation: Operation, prefix: "Optional[int]") -> "List[str]":
    found: "List[str]" = []
    limit = len(operation.effects) if prefix is None else prefix
    for effect in operation.effects[:limit]:
        if indeterminate_records(world.store) and not operation.tolerates_indeterminate:
            world.read_after_indeterminate.append(f"{operation.code}:{type(effect).__name__}")
        apply_effect(world, effect)
        for violation in violations(world, effect):
            if violation not in found:
                found.append(violation)
    return found


def _stage(world: World, operation: Operation) -> None:
    """Build state for a scenario. A refused or violating stage raises rather than being ignored:
    a silently refused stage builds a world the scenario then tests for the wrong reason."""
    outcome = perform(world, operation)
    if outcome.refused:
        raise AssertionError(f"stage {operation.name} ({operation.code}) refused: {outcome.refusal}")
    if outcome.violations:
        raise AssertionError(f"stage {operation.name} ({operation.code}) violated {outcome.violations}")


# --------------------------------------------------------------------------------------------------
# The invariants, stated over the world (§21.2)
# --------------------------------------------------------------------------------------------------


def _receipt_is_sound(world: World, task: str, receipt: object) -> bool:
    """I4's half that the real validator cannot check: the anchor is a receipt over a published intent."""
    if not isinstance(receipt, str) or not receipt.startswith("rcp-"):
        return False
    for record in world.store.of_kind("acceptance"):
        if record.get("receipt_id") != receipt or record.get("task") != task:
            continue
        if record.get("intent") != "DONE":
            continue
        recomputed = receipt_for(
            record["accepted_snapshot"], str(record["attempt"]), str(record["definition_hash"]),
            str(record["intent"]), list(record["qualifying_captures"]),
        )
        if recomputed == receipt:
            return True
    return False


def violations(world: World, last: Effect) -> "List[str]":
    """I1 to I5 over the modelled world, checked after every durable effect."""
    found: "List[str]" = []

    holder: "Dict[str, str]" = {}
    for attempt, row in sorted(active_grants(world.store).items()):
        for claim in row["claims"]:
            other = holder.get(claim)
            if other is not None and other != attempt:
                found.append(f"I1 {claim} is held by {other} and {attempt}")
            holder[claim] = attempt

    for record in world.store.of_kind("release"):
        attempt = str(record["attempt"])
        if can_write(world, attempt):
            found.append(f"I2 {attempt} released a claim while it can still write")

    if isinstance(last, CommitCanonical):
        # I3 **here**, not at the intent: everything the mutation is exposed to happens in between.
        if digest(world.definition) != last.definition_hash:
            found.append(f"I3 the definition moved under the commit of {last.task}")
        subjects = sorted(last.accepted_snapshot)
        if last.accepted_snapshot != snapshot_of(world, subjects):
            found.append(f"I3 the bytes moved under the commit of {last.task}")

    for task, entry in sorted(world.store.canonical.items()):
        if entry.get("status") != "DONE":
            continue
        if not any(_receipt_is_sound(world, task, r) for r in entry.get("evidence", [])):
            found.append(f"I4 {task} is DONE with no receipt that recomputes from a published intent")

    for path in sorted(world.store.records):
        if projection(world.store, path) == "absent":
            found.append(f"I5 {path} exists and projects as absent")
    for site in world.read_after_indeterminate:
        found.append(f"I5 an effect was applied over an indeterminate store at {site}")

    return found


# --------------------------------------------------------------------------------------------------
# Recovery: §8.2's completions, as operations
# --------------------------------------------------------------------------------------------------


def uncertain_start(store: Store, attempt: str) -> bool:
    """§6.1: the adapter answered `ambiguous`, or the caller died inside `start` (§8.2, O4 after 5)."""
    record = store.records.get(f"attempts/{attempt}/launch.json", MISSING)
    if isinstance(record, dict):
        return str(record.get("start_outcome")) == "ambiguous"
    return capability(store, attempt) != "none"


def _dispatch_completion(world: World, attempt: str, coordinator: str,
                         intent_record: "Dict[str, Any]") -> "Optional[Operation]":
    """§8.2's O4 row, all seven prefixes, built by dropping the steps the store already shows.

    Deriving the suffix from `op_dispatch`'s own effect list rather than from a second hand-written
    list is what makes a completion byte-identical to the interrupted original; a re-derived record
    that merely resembled the first would be a `conflict`, not an `identical` (§7.2).
    """
    store = world.store
    task = str(intent_record["task"])
    stored = str(intent_record["receipt_id"])
    if digest(world.definition) != str(intent_record["definition_hash"]):
        # The definition moved under an unfinished dispatch: revalidation is the whole answer, and it
        # is reached by proposing the operation with no effects rather than by deciding here.
        return Operation("complete-dispatch", "O4", [],
                         guard=lambda w: revalidate(w, attempt, task, stored))

    granted = attempt in store.registry
    if not granted:
        # Nothing O4 can still do. Every step from 5 onward needs the grant, and the only operation
        # that removes a grant is O16 (or O18), both of which run after O4 is over — so an intent with
        # no grant is a dispatch that finished or an attempt that was disposed, never one in flight.
        return None

    template = op_dispatch(attempt, coordinator, task)
    directory = mutation_dir(attempt, stored)
    launch_path = f"attempts/{attempt}/launch.json"
    committed = store.has(f"{directory}/ack.json") or \
        store.canonical.get(task, {}).get("status", "TODO") != "TODO"
    issued = capability(store, attempt) != "none"
    ambiguous = issued and not store.has(launch_path)
    effects: "List[Effect]" = []
    for effect in template.effects:
        if isinstance(effect, CommitCanonical):
            # The ack, not the status, is what proves the commit landed. O13's `RUNNING → TODO` puts
            # the task back in `TODO`, so a status test alone would re-dispatch a withdrawn task
            # (§8.3's third bullet).
            if committed:
                continue
        elif isinstance(effect, Registry):
            if effect.action == "issue" and issued:
                continue
            if effect.action == "consume" and capability(store, attempt) == "consumed":
                continue
        elif isinstance(effect, Start):
            # §8.2, O4 after 5, and after 6 before 7: the capability is issued, so whether the adapter
            # ran is unknowable. Never re-issue it and never call `start` again.
            if issued:
                continue
        elif isinstance(effect, Publish):
            if store.has(effect.path):
                continue
            if effect.path == launch_path:
                if ambiguous:
                    effect = Publish(launch_path, dict(effect.record, start_outcome="ambiguous"))
        effects.append(effect)
    if not effects:
        return None
    return Operation("complete-dispatch", "O4", effects,
                     guard=lambda w: revalidate(w, attempt, task, stored))


def _commit_completion(world: World, attempt: str, coordinator: str,
                       intent_record: "Dict[str, Any]") -> "Optional[Operation]":
    """§8.2's O12/O13 rows: fence-and-continue, re-run the commit, or ack — whichever is missing."""
    store = world.store
    task = str(intent_record["task"])
    receipt = str(intent_record["receipt_id"])
    status = str(intent_record["intent"])
    directory = mutation_dir(attempt, receipt)
    landed = receipt in store.canonical.get(task, {}).get("evidence", [])
    fences = sorted(p for p in store.records if p.startswith(f"{directory}/fences/"))
    # §8.2's O12 "after 2" is *re-run step 3*, reusing the fence that is already there. A new fence is
    # what a lost revision race needs (§8.3), not what a crash prefix needs.
    template = op_commit_acceptance(
        attempt, coordinator, task, receipt, dict(intent_record["accepted_snapshot"]),
        str(intent_record["definition_hash"]), status=status, fence=max(len(fences), 1),
    )
    effects: "List[Effect]" = []
    for effect in template.effects:
        if isinstance(effect, AppendEvidence):
            if landed or status != "DONE" or effect.receipt in store.evidence_log:
                continue
        elif isinstance(effect, CommitCanonical):
            if landed:
                continue
        elif isinstance(effect, Publish):
            if store.has(effect.path):
                continue
            if "/fences/" in effect.path and landed:
                continue
        effects.append(effect)
    if not effects:
        return None
    guard = None if landed else (lambda w: revalidate(w, attempt, task, receipt))
    return Operation("complete-commit", "O12", effects, guard=guard)


def recover(world: World) -> "List[Operation]":
    """Read the world, return the operations §8.2 says complete it. Nothing here applies an effect.

    It reads the world and not only the store because O3's completion re-reads the baseline digests,
    which are bytes rather than records.
    """
    store = world.store
    coordinator = str(store.ownership["coordinator_run"])
    generation = int(store.ownership["generation"])

    # §12.4 first and alone: while any earlier-generation record is unexplained the store is
    # indeterminate, so every other completion would be reading past an unreadable record (I5).
    fences: "List[Effect]" = [
        effect for effect in generation_fence_effects(store, coordinator, generation)
        if isinstance(effect, Publish) and not store.has(effect.path)
    ]
    if fences:
        return [Operation("complete-take-over", "O17", fences, tolerates_indeterminate=True)]

    operations: "List[Operation]" = []

    for record in store.of_kind("reservation"):
        attempt = str(record["attempt"])
        if attempt in store.registry or store.has(f"attempts/{attempt}/release.json"):
            continue
        operations.append(op_grant(attempt, coordinator, str(record["task"]), record["claims"][0],
                                   str(record["definition_hash"])))

    for attempt, row in sorted(active_grants(store).items()):
        if store.has(f"attempts/{attempt}/prepared.json"):
            continue
        claim = row["claims"][0]
        operations.append(op_prepare(attempt, coordinator, str(row["task"]), claim,
                                     snapshot_of(world, [claim])[claim]))

    for record in store.of_kind("scope"):
        if record.get("scope_kind") != "check":
            continue
        attempt = str(record["attempt"])
        if str(record["scope_id"]) in sealed_scopes(store, attempt):
            continue
        claim = record["claims"][0]
        operations.append(op_run_check(attempt, coordinator, claim, snapshot_of(world, [claim])[claim]))

    for record in store.of_kind("acceptance"):
        attempt = str(record["attempt"])
        if str(record["intent"]) == "RUNNING":
            operation = _dispatch_completion(world, attempt, coordinator, record)
        else:
            operation = _commit_completion(world, attempt, coordinator, record)
        if operation is not None:
            operations.append(operation)

    held = {(str(r["cause_id"]), str(r["attempt"])) for r in store.of_kind("hold")}
    for record in store.of_kind("stop-request"):
        cause = stop_cause_id(int(record["sequence"]))
        for attempt in sorted(active_grants(store)):
            if (cause, attempt) in held:
                continue
            task = str(store.registry[attempt]["task"])
            operations.append(op_hold(attempt, coordinator, task, cause, "operator_stop"))

    for record in store.of_kind("release"):
        attempt = str(record["attempt"])
        if attempt in store.registry:
            # The one prefix where the journal is ahead of the registry (§8.2, O16 after 1).
            operations.append(Operation("complete-release", "O16", [Registry("remove", attempt)],
                                        guard=lambda w, a=attempt: refuse_release(w, a)))

    for record in store.of_kind("sealed"):
        if record.get("exit") != "not_started":
            continue
        attempt = str(record["attempt"])
        if store.has(f"attempts/{attempt}/release.json"):
            continue
        operations.append(Operation("complete-dispose", "O18",
                                    op_dispose(attempt, coordinator).effects[1:],
                                    guard=lambda w, a=attempt: refuse_dispose(w, a)))

    return operations


def _signature(world: World) -> str:
    store = world.store
    return digest({
        "records": sorted(store.records),
        "registry": sorted((a, str(r.get("capability"))) for a, r in store.registry.items()),
        "canonical": sorted(
            (t, str(e.get("status")), list(e.get("evidence", []))) for t, e in store.canonical.items()
        ),
        "ownership": [str(store.ownership["coordinator_run"]), int(store.ownership["generation"])],
        "evidence_log": list(store.evidence_log),
    })


def run(world: World, operation: Operation, prefix: "Optional[int]" = None) -> "List[str]":
    """Apply one operation and report findings. A refusal is a finding, not a silence."""
    outcome = perform(world, operation, prefix)
    if outcome.refused:
        return [f"refused {operation.code}: {outcome.refusal}"]
    return outcome.violations


def drain(world: World, rounds: int = 8) -> "List[str]":
    """Recover until nothing is left to do. Findings accumulate; a stuck recovery is reported."""
    found: "List[str]" = []
    for _ in range(rounds):
        operations = recover(world)
        if not operations:
            return found
        before = _signature(world)
        for operation in operations:
            for item in run(world, operation):
                if item not in found:
                    found.append(item)
        if _signature(world) == before:
            # Recovery proposed work and none of it applied: every step was refused. That is a
            # fixpoint too, and the refusals are already in `found`.
            return found
    return [*found, "recovery did not reach a fixpoint"]


# --------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------


def _run_stage(world: World, stage: str) -> None:
    if stage == "reserve":
        _stage(world, op_reserve(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION)))
    elif stage == "grant":
        _stage(world, op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION)))
    elif stage == "prepare":
        _stage(world, op_prepare(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT,
                                 snapshot_of(world, [SUBJECT])[SUBJECT]))
    elif stage == "dispatch":
        _stage(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK))
    elif stage == "write":
        world.content[SUBJECT] = "the modelled worker's output\n"
    elif stage == "exit":
        world.running.discard(ATTEMPT_A)
    elif stage == "result":
        _stage(world, op_record_result(ATTEMPT_A, COORDINATOR_A, SUBJECT,
                                      snapshot_of(world, [SUBJECT])[SUBJECT]))
    elif stage == "check":
        _stage(world, op_run_check(ATTEMPT_A, COORDINATOR_A, SUBJECT,
                                   snapshot_of(world, [SUBJECT])[SUBJECT]))
    elif stage == "seal":
        _stage(world, op_seal(ATTEMPT_A, COORDINATOR_A, "worker"))
    elif stage == "classify":
        _stage(world, op_classify(ATTEMPT_A, COORDINATOR_A, TASK))
    elif stage == "accept":
        _stage(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT,
                                accept_receipt(world, ATTEMPT_A, SUBJECT),
                                snapshot_of(world, [SUBJECT]), digest(world.definition)))
    elif stage == "commit":
        _stage(world, op_commit_acceptance(ATTEMPT_A, COORDINATOR_A, TASK,
                                           accept_receipt(world, ATTEMPT_A, SUBJECT),
                                           snapshot_of(world, [SUBJECT]), digest(world.definition)))
    elif stage == "hold":
        _stage(world, op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "adapter_error"))
    elif stage == "resolve-withdraw":
        _stage(world, op_resolve(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "withdraw"))
    else:
        raise AssertionError(f"unknown stage {stage}")


def staged(*stages: str, adapter: Adapter = SEALING) -> World:
    world = World(adapter=adapter)
    world.store.canonical[TASK] = {"status": "TODO", "evidence": []}
    world.store.canonical[OTHER_TASK] = {"status": "TODO", "evidence": []}
    for stage in stages:
        _run_stage(world, stage)
    return world


READY = ("reserve", "grant", "prepare", "dispatch", "write", "exit", "result", "check", "seal",
         "classify")
ACCEPTED = (*READY, "accept", "commit")

# Each row is a name, the stages that build the world, the operation under test, and the crash
# prefixes after which the store legitimately differs from the uncrashed one. Only O4 has any: §8.2
# says a crash after step 5 makes the start ambiguous, and an ambiguous start is recorded as such
# rather than retried.
SCENARIOS: "Tuple[Tuple[str, Tuple[str, ...], Callable[[World], Operation], Tuple[int, ...]], ...]" = (
    ("reserve", (), lambda w: op_reserve(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION)), ()),
    ("grant", ("reserve",),
     lambda w: op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION)), ()),
    ("prepare", ("reserve", "grant"),
     lambda w: op_prepare(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, snapshot_of(w, [SUBJECT])[SUBJECT]),
     ()),
    ("dispatch", ("reserve", "grant", "prepare"),
     lambda w: op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK), (5, 6)),
    ("record-result", ("reserve", "grant", "prepare", "dispatch", "write", "exit"),
     lambda w: op_record_result(ATTEMPT_A, COORDINATOR_A, SUBJECT, snapshot_of(w, [SUBJECT])[SUBJECT]),
     ()),
    ("run-check", ("reserve", "grant", "prepare", "dispatch", "write", "exit", "result"),
     lambda w: op_run_check(ATTEMPT_A, COORDINATOR_A, SUBJECT, snapshot_of(w, [SUBJECT])[SUBJECT]), ()),
    ("seal", ("reserve", "grant", "prepare", "dispatch", "write", "exit", "result", "check"),
     lambda w: op_seal(ATTEMPT_A, COORDINATOR_A, "worker"), ()),
    ("classify", READY[:-1], lambda w: op_classify(ATTEMPT_A, COORDINATOR_A, TASK), ()),
    ("accept", READY,
     lambda w: op_accept(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, accept_receipt(w, ATTEMPT_A, SUBJECT),
                         snapshot_of(w, [SUBJECT]), digest(w.definition)), ()),
    ("commit-acceptance", (*READY, "accept"),
     lambda w: op_commit_acceptance(ATTEMPT_A, COORDINATOR_A, TASK, accept_receipt(w, ATTEMPT_A, SUBJECT),
                                    snapshot_of(w, [SUBJECT]), digest(w.definition)), ()),
    ("release", ACCEPTED,
     lambda w: op_release(ATTEMPT_A, COORDINATOR_A, accept_receipt(w, ATTEMPT_A, SUBJECT)), ()),
    ("hold", READY, lambda w: op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "adapter_error"), ()),
    ("resolve", (*READY, "hold"),
     lambda w: op_resolve(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "accept"), ()),
    ("withdraw", (*READY, "hold", "resolve-withdraw"),
     lambda w: op_withdraw(ATTEMPT_A, COORDINATOR_A, TASK, digest(w.definition)), ()),
    ("stop", READY, lambda w: op_request_stop(COORDINATOR_A, 1, w.store), ()),
    ("take-over", READY,
     lambda w: op_take_over(w.store, COORDINATOR_B, COORDINATOR_A, "pid_gone", 1), ()),
    ("dispose", ("reserve", "grant", "prepare"), lambda w: op_dispose(ATTEMPT_A, COORDINATOR_A), ()),
)


def refused(world: World, operation: Operation) -> "Optional[str]":
    return perform(world, operation).refusal


def store_root_fence_covers(store: Store, path: str, generation: int) -> bool:
    """§12.4: the project-level paths are explained by the fence at the store root, not by an attempt's.

    A separate predicate rather than a reuse of `fenced_paths`, because the point being asserted is
    *which* fence covers a project-level path — an attempt's fence listing `control/...` would satisfy
    `fenced_paths` and would still be a fence whose scope does not contain the record.
    """
    for record in store.of_kind("generation-fence"):
        if record.get("scope") != "project" or int(record["generation"]) != generation:
            continue
        if path in record["records"]:
            return True
    return False


# --------------------------------------------------------------------------------------------------
# The real validator (I4's schema half)
# --------------------------------------------------------------------------------------------------


def _task_record(task_id: str, status: str, evidence: "List[Dict[str, Any]]") -> "Dict[str, Any]":
    return {
        "id": task_id,
        "name": f"Model task {task_id}",
        "status": status,
        "depends_on": [],
        "outputs": [],
        "success_criteria": "the modelled subject is non-empty",
        "verification": "the modelled checker reports exit 0",
        "evidence": evidence,
        "effect": {"kind": "local_write", "description": "writes the modelled subject"},
        "authorization": {
            "required": False, "status": "not_required", "scope": None,
            "source": None, "authorized_at": None,
        },
        "receipts": [],
        "skip_reason": None,
        "block_reason": None,
    }


def build_candidate(world: World, baseline: "Dict[str, Any]") -> "Dict[str, Any]":
    """The real v3 candidate a coordinator would commit from this modelled canonical state."""
    candidate = copy.deepcopy(baseline)
    tasks = []
    for task_id in (TASK, OTHER_TASK):
        entry = world.store.canonical.get(task_id, {"status": "RUNNING", "evidence": []})
        evidence = [
            {"root": "workspace", "path": "evidence.md", "anchor": receipt}
            for receipt in entry.get("evidence", [])
        ]
        tasks.append(_task_record(task_id, entry.get("status", "RUNNING"), evidence))
    candidate["tasks"] = tasks
    candidate["current_tasks"] = [t["id"] for t in tasks if t["status"] == "RUNNING"]
    return candidate


class RealValidatorHarness:
    """One real project directory, reused so I4 costs file I/O once rather than once per assertion."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        self.project_dir = allocate_project(
            root, title="Parallel execution reference model", working_directory=Path(self.tmp.name)
        )
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state["status"] = "EXECUTING"
        state["tasks"] = [_task_record(TASK, "RUNNING", []), _task_record(OTHER_TASK, "RUNNING", [])]
        state["current_tasks"] = [TASK, OTHER_TASK]
        (self.project_dir / "project.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
        )
        self.baseline = state

    def commit(self, candidate: "Dict[str, Any]") -> None:
        """Make this candidate the baseline, so the next check sees a real DONE→X transition."""
        landed = copy.deepcopy(candidate)
        landed["revision"] = candidate["revision"] + 1
        (self.project_dir / "project.json").write_text(
            json.dumps(landed, indent=2) + "\n", encoding="utf-8"
        )
        self.baseline = landed

    def errors(self, candidate: "Dict[str, Any]") -> "List[str]":
        path = self.project_dir / "candidate.json"
        path.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        report = check_candidate(self.project_dir, path, expected_revision=self.baseline["revision"])
        return list(report.errors)

    def close(self) -> None:
        self.tmp.cleanup()


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------


class CrashPrefixTests(unittest.TestCase):
    """A crash prefix is a proper, non-empty prefix of an operation's durable effects.

    Prefix 0 is excluded deliberately: an operation that produced no durable effect leaves recovery
    nothing to reconstruct, and §8.2's rows all begin "after 1".
    """

    def _reference(self, stages: "Tuple[str, ...]",
                   builder: "Callable[[World], Operation]") -> "Tuple[World, List[str]]":
        world = staged(*stages)
        findings = run(world, builder(world))
        findings.extend(drain(world))
        return world, findings

    def test_no_prefix_violates_an_invariant(self) -> None:
        for name, stages, builder, _ in SCENARIOS:
            with self.subTest(scenario=name):
                world, findings = self._reference(stages, builder)
                self.assertEqual(findings, [], name)
                self.assertEqual(world.store.conflicts, [], name)

    def test_recovery_of_a_complete_operation_is_a_no_op(self) -> None:
        for name, stages, builder, _ in SCENARIOS:
            with self.subTest(scenario=name):
                world, _ = self._reference(stages, builder)
                before = _signature(world)
                self.assertEqual(drain(world), [], name)
                self.assertEqual(_signature(world), before, name)

    def test_every_prefix_recovers_to_the_uncrashed_store(self) -> None:
        for name, stages, builder, ambiguous in SCENARIOS:
            reference, _ = self._reference(stages, builder)
            probe = staged(*stages)
            count = len(builder(probe).effects)
            for prefix in range(1, count):
                with self.subTest(scenario=name, prefix=prefix):
                    world = staged(*stages)
                    self.assertEqual(run(world, builder(world), prefix), [], f"{name}/{prefix}")
                    self.assertEqual(drain(world), [], f"{name}/{prefix}")
                    self.assertEqual(world.store.conflicts, [], f"{name}/{prefix}")
                    self.assertEqual(world.store.canonical, reference.store.canonical)
                    self.assertEqual(world.store.registry, reference.store.registry)
                    if prefix in ambiguous:
                        self.assertEqual(sorted(world.store.records),
                                         sorted(reference.store.records))
                        launch = world.store.records[f"attempts/{ATTEMPT_A}/launch.json"]
                        self.assertEqual(launch["start_outcome"], "ambiguous")
                        self.assertTrue(uncertain_start(world.store, ATTEMPT_A))
                    else:
                        self.assertEqual(world.store.records, reference.store.records)
                        self.assertFalse(uncertain_start(world.store, ATTEMPT_A)
                                         and "dispatch" not in name)

    def test_a_crash_before_the_spawn_does_not_free_the_subject(self) -> None:
        """R4-01: the capability is issued at step 5 and the spawn fires at step 6, so a crash in
        between leaves a write authority nothing has observed."""
        world = staged("reserve", "grant", "prepare")
        self.assertEqual(run(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK), 5), [])
        self.assertTrue(can_write(world, ATTEMPT_A))
        self.assertEqual(refused(world, op_release(ATTEMPT_A, COORDINATOR_A, None)),
                         "R-LAUNCH-IN-FLIGHT")
        self.assertTrue(any(v.startswith("I2") for v in
                            force(world, op_release(ATTEMPT_A, COORDINATOR_A, None))))


class ForcedViolationTests(unittest.TestCase):
    """Every invariant is shown to have a reachable failure, through the separate `force` API."""

    def test_i1_fires_when_two_grants_cover_one_subject(self) -> None:
        world = staged("reserve", "grant")
        found = force(world, op_grant(ATTEMPT_B, COORDINATOR_B, OTHER_TASK, SUBJECT, digest(DEFINITION)))
        self.assertTrue(any(v.startswith("I1") for v in found), found)

    def test_i2_fires_when_an_armed_launch_is_released(self) -> None:
        world = staged("reserve", "grant", "prepare")
        run(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK), 5)
        found = force(world, op_release(ATTEMPT_A, COORDINATOR_A, None))
        self.assertTrue(any(v.startswith("I2") for v in found), found)

    def test_i3_fires_when_the_bytes_moved_under_the_acceptance(self) -> None:
        world = staged(*READY)
        receipt = accept_receipt(world, ATTEMPT_A, SUBJECT)
        snapshot = snapshot_of(world, [SUBJECT])
        world.content[SUBJECT] = "something else entirely\n"
        found = force(world, op_commit_acceptance(ATTEMPT_A, COORDINATOR_A, TASK, receipt, snapshot,
                                                 digest(world.definition)))
        self.assertIn(f"I3 the bytes moved under the commit of {TASK}", found)

    def test_i3_fires_when_the_definition_moved_under_the_acceptance(self) -> None:
        world = staged(*READY)
        receipt = accept_receipt(world, ATTEMPT_A, SUBJECT)
        snapshot = snapshot_of(world, [SUBJECT])
        stale = digest(world.definition)
        world.definition[TASK]["instruction"] = "write a different out.txt"
        found = force(world, op_commit_acceptance(ATTEMPT_A, COORDINATOR_A, TASK, receipt, snapshot,
                                                 stale))
        self.assertIn(f"I3 the definition moved under the commit of {TASK}", found)

    def test_i4_fires_when_a_task_is_done_without_a_receipt(self) -> None:
        world = staged(*READY)
        forged = Operation("forge", "none", [
            CommitCanonical(TASK, "DONE", "rcp-bogus", {}, digest(world.definition), "DONE"),
        ])
        found = force(world, forged)
        self.assertIn(f"I4 {TASK} is DONE with no receipt that recomputes from a published intent",
                      found)

    def test_i5_fires_when_an_effect_is_applied_over_an_unreadable_record(self) -> None:
        world = staged("reserve")
        world.store.records[f"attempts/{ATTEMPT_A}/result.json"] = None
        found = force(world, op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION)))
        self.assertTrue(any(v.startswith("I5 an effect was applied") for v in found), found)


class ProjectionTests(unittest.TestCase):
    """§6.3: a record that exists but cannot be read is `indeterminate`, never `absent`."""

    def setUp(self) -> None:
        self.world = staged()
        self.path = f"attempts/{ATTEMPT_A}/reservation.json"

    def _reservation(self) -> "Dict[str, Any]":
        return {
            "kind": "reservation", "attempt": ATTEMPT_A, "coordinator": COORDINATOR_A, "task": TASK,
            "claims": [SUBJECT], "definition_hash": digest(DEFINITION), "written_in": 0,
        }

    def test_a_missing_record_is_absent(self) -> None:
        self.assertEqual(projection(self.world.store, self.path), "absent")

    def test_a_well_formed_record_is_present(self) -> None:
        self.world.store.records[self.path] = self._reservation()
        self.assertEqual(projection(self.world.store, self.path), "present")

    def test_a_record_missing_a_required_field_is_indeterminate(self) -> None:
        record = self._reservation()
        del record["claims"]
        self.world.store.records[self.path] = record
        self.assertEqual(projection(self.world.store, self.path), "indeterminate")

    def test_an_unknown_kind_is_indeterminate(self) -> None:
        self.world.store.records[self.path] = dict(self._reservation(), kind="death-proof")
        self.assertEqual(projection(self.world.store, self.path), "indeterminate")

    def test_a_non_object_record_is_indeterminate(self) -> None:
        self.world.store.records[self.path] = None
        self.assertEqual(projection(self.world.store, self.path), "indeterminate")
        self.assertEqual(refuse_common(self.world), "R-INDETERMINATE")

    def test_an_unexplained_generation_is_indeterminate(self) -> None:
        self.world.store.records[self.path] = dict(self._reservation(), written_in=7)
        self.assertEqual(projection(self.world.store, self.path), "indeterminate")


class GuardTests(unittest.TestCase):
    """Refusal codes over facts (§4). Every case here reads records, never a modelled process."""

    def test_a_resumable_adapter_is_refused_at_admission(self) -> None:
        world = staged(adapter=RESUMABLE)
        self.assertEqual(
            refused(world, op_reserve(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION))),
            "R-DETACHED-CHILD",
        )

    def test_a_held_subject_refuses_a_second_reservation(self) -> None:
        world = staged("reserve", "grant")
        self.assertEqual(
            refused(world, op_reserve(ATTEMPT_B, COORDINATOR_A, OTHER_TASK, SUBJECT,
                                      digest(DEFINITION))),
            "R-CLAIM-CONFLICT",
        )

    def test_a_grant_without_a_reservation_is_refused(self) -> None:
        world = staged()
        self.assertEqual(
            refused(world, op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION))),
            "R-NO-RESERVATION",
        )

    def test_a_second_capability_is_refused(self) -> None:
        world = staged("reserve", "grant", "prepare", "dispatch")
        self.assertEqual(refused(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK)),
                         "R-CAPABILITY-ISSUED")

    def test_a_capability_without_a_grant_is_refused(self) -> None:
        world = staged("reserve", "grant", "prepare")
        world.store.registry.pop(ATTEMPT_A)
        self.assertEqual(refused(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK)), "R-NO-GRANT")

    def test_takeover_without_a_proof_of_death_is_refused(self) -> None:
        world = staged(*READY)
        self.assertEqual(
            refused(world, op_take_over(world.store, COORDINATOR_B, COORDINATOR_A, "stale_heartbeat", 1)),
            "R-NO-PROOF-OF-DEATH",
        )
        self.assertEqual(
            refused(world, op_take_over(world.store, COORDINATOR_B, COORDINATOR_B, "pid_gone", 1)),
            "R-NOT-THE-OWNER",
        )

    def test_a_ready_attempt_accepts(self) -> None:
        world = staged(*READY)
        operation = op_accept(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT,
                              accept_receipt(world, ATTEMPT_A, SUBJECT),
                              snapshot_of(world, [SUBJECT]), digest(world.definition))
        self.assertIsNone(refused(world, operation))


class ReviewFourRegressionTests(unittest.TestCase):
    """One case per review-04 finding this model can express as a trace.

    R4-04's case is the take-over acquisition, not O19's: O19 needs two live coordinator processes,
    which this model does not have, and the orphan-prefix property it asked about is the same one —
    a crash between the ownership commit and the records that explain the generation it opened.
    """

    def test_r4_01_a_delayed_launch_cannot_outlive_its_reservation(self) -> None:
        world = staged("reserve", "grant", "prepare")
        run(world, op_dispatch(ATTEMPT_A, COORDINATOR_A, TASK), 5)
        self.assertTrue(can_write(world, ATTEMPT_A))
        self.assertEqual(refused(world, op_release(ATTEMPT_A, COORDINATOR_A, None)),
                         "R-LAUNCH-IN-FLIGHT")

    def test_r4_02_a_superseded_coordinator_cannot_clobber_a_journal_record(self) -> None:
        world = staged(*READY)
        self.assertEqual(run(world, op_take_over(world.store, COORDINATOR_B, COORDINATOR_A,
                                                 "pid_gone", 1)), [])
        original = copy.deepcopy(world.store.records[f"attempts/{ATTEMPT_A}/result.json"])
        ghost = op_record_result(ATTEMPT_A, COORDINATOR_A, SUBJECT, digest("a ghost's bytes\n"))
        force(world, ghost)
        self.assertEqual(world.store.records[f"attempts/{ATTEMPT_A}/result.json"], original)
        self.assertIn(f"attempts/{ATTEMPT_A}/result.json", world.store.conflicts)

    def test_r4_03_a_finished_worker_is_not_a_sealed_one(self) -> None:
        world = staged("reserve", "grant", "prepare", "dispatch", "write", "exit", "result",
                       adapter=SEALING)
        self.assertNotIn(ATTEMPT_A, world.running)
        self.assertEqual(refused(world, op_release(ATTEMPT_A, COORDINATOR_A, None)),
                         "R-LAUNCH-IN-FLIGHT")

    def test_r4_03_sealing_is_what_makes_a_release_safe(self) -> None:
        world = staged(*ACCEPTED)
        self.assertIsNone(refused(world, op_release(ATTEMPT_A, COORDINATOR_A,
                                                    accept_receipt(world, ATTEMPT_A, SUBJECT))))

    def test_r4_04_the_acquisition_orphan_prefix_completes(self) -> None:
        world = staged(*READY)
        operation = op_take_over(world.store, COORDINATOR_B, COORDINATOR_A, "pid_gone", 1)
        self.assertEqual(run(world, operation, 1), [])
        self.assertTrue(indeterminate_records(world.store))
        self.assertEqual(drain(world), [])
        self.assertEqual(indeterminate_records(world.store), [])

    def test_r4_06_the_dispatch_gate_opens_on_resolution_not_classification(self) -> None:
        world = staged(*READY)
        _stage(world, op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "adapter_error"))
        reserve_b = op_reserve(ATTEMPT_B, COORDINATOR_A, OTHER_TASK, OTHER_SUBJECT, digest(DEFINITION))
        self.assertEqual(refused(world, reserve_b), "R-GATE-CLOSED")
        _stage(world, op_resolve(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "accept"))
        self.assertIsNone(refused(world, reserve_b))

    def test_r4_06_an_operator_stop_does_not_close_the_gate(self) -> None:
        world = staged(*READY)
        _stage(world, op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "stop-0001", "operator_stop"))
        self.assertEqual(open_causes(world.store), set())
        reserve_b = op_reserve(ATTEMPT_B, COORDINATOR_A, OTHER_TASK, OTHER_SUBJECT, digest(DEFINITION))
        self.assertIsNone(refused(world, reserve_b))

    def test_r4_07_evidence_is_bound_to_the_accepted_bytes(self) -> None:
        world = staged(*READY, "accept")
        receipt = accept_receipt(world, ATTEMPT_A, SUBJECT)
        world.content[SUBJECT] = "a later writer got there first\n"
        self.assertEqual(revalidate(world, ATTEMPT_A, TASK, receipt), "stale_evidence")

    def test_r4_08_a_changed_definition_withdraws_instead_of_accepting(self) -> None:
        world = staged(*READY)
        world.definition[TASK]["instruction"] = "write a different out.txt"
        operation = op_accept(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT,
                              accept_receipt(world, ATTEMPT_A, SUBJECT),
                              snapshot_of(world, [SUBJECT]), digest(world.definition))
        self.assertEqual(refused(world, operation), "definition_changed")
        _stage(world, op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "c-2", "definition_changed"))
        _stage(world, op_resolve(ATTEMPT_A, COORDINATOR_A, TASK, "c-2", "withdraw"))
        self.assertEqual(
            run(world, op_withdraw(ATTEMPT_A, COORDINATOR_A, TASK, digest(world.definition))), [])
        self.assertEqual(world.store.canonical[TASK]["status"], "TODO")

    def test_r4_12_identical_bytes_are_success_and_different_bytes_are_an_event(self) -> None:
        world = staged("reserve")
        path = f"attempts/{ATTEMPT_A}/reservation.json"
        record = {
            "kind": "reservation", "attempt": ATTEMPT_A, "coordinator": COORDINATOR_A, "task": TASK,
            "claims": [SUBJECT], "definition_hash": digest(DEFINITION),
        }
        self.assertEqual(world.store.publish(path, record), "identical")
        self.assertEqual(world.store.publish(path, dict(record, claims=[OTHER_SUBJECT])), "conflict")
        self.assertEqual(world.store.records[path]["claims"], [SUBJECT])

    def test_r4_14_a_commit_is_proved_by_the_receipt_not_by_a_status(self) -> None:
        world = staged(*READY, "accept")
        receipt = accept_receipt(world, ATTEMPT_A, SUBJECT)
        forged = Operation("forge", "none", [
            CommitCanonical(TASK, "DONE", "rcp-" + "0" * 32, snapshot_of(world, [SUBJECT]),
                            digest(world.definition), "DONE"),
        ])
        self.assertTrue(any(v.startswith("I4") for v in force(world, forged)))
        world = staged(*READY, "accept")
        self.assertEqual(run(world, op_commit_acceptance(
            ATTEMPT_A, COORDINATOR_A, TASK, receipt, snapshot_of(world, [SUBJECT]),
            digest(world.definition))), [])


class ReviewFiveRegressionTests(unittest.TestCase):
    """One named case per R5-07 probe, plus the two defects writing this model found in revision 6."""

    def test_r5_07_a_stored_null_is_not_absence(self) -> None:
        world = staged("reserve")
        world.store.records[f"attempts/{ATTEMPT_A}/prepared.json"] = None
        self.assertEqual(projection(world.store, f"attempts/{ATTEMPT_A}/prepared.json"),
                         "indeterminate")
        self.assertEqual(
            refused(world, op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, digest(DEFINITION))),
            "R-INDETERMINATE",
        )

    def test_r5_07_a_result_without_a_seal_refuses_release(self) -> None:
        world = staged("reserve", "grant", "prepare", "dispatch", "write", "exit", "result")
        self.assertTrue(world.adapter.seals)
        self.assertEqual(refused(world, op_release(ATTEMPT_A, COORDINATOR_A, None)),
                         "R-LAUNCH-IN-FLIGHT")

    def test_r5_07_recovery_revalidates_before_it_commits(self) -> None:
        world = staged(*READY, "accept")
        world.content[SUBJECT] = "a later writer got there first\n"
        findings = drain(world)
        self.assertTrue(any("stale_evidence" in f for f in findings), findings)
        self.assertEqual(world.store.canonical[TASK]["status"], "RUNNING")

    def test_r5_07_a_done_task_needs_a_receipt_not_an_anchor(self) -> None:
        world = staged(*READY, "accept")
        forged = Operation("forge", "none", [
            CommitCanonical(TASK, "DONE", "not-a-receipt", snapshot_of(world, [SUBJECT]),
                            digest(world.definition), "DONE"),
        ])
        self.assertTrue(any(v.startswith("I4") for v in force(world, forged)))

    def test_r5_07_an_unresolved_failure_closes_the_gate_for_every_task(self) -> None:
        world = staged(*READY)
        _stage(world, op_hold(ATTEMPT_A, COORDINATOR_A, TASK, "c-1", "adapter_error"))
        self.assertEqual(
            refused(world, op_reserve(ATTEMPT_B, COORDINATOR_A, OTHER_TASK, OTHER_SUBJECT,
                                      digest(DEFINITION))),
            "R-GATE-CLOSED",
        )

    def test_a_takeover_does_not_lose_a_durable_stop_request(self) -> None:
        """The store-root generation fence, which revision 6 did not have: an attempt's fence can only
        explain paths under that attempt, so nothing could explain `control/stop-requests/0001.json`."""
        world = staged(*READY)
        _stage(world, op_request_stop(COORDINATOR_A, 1, world.store))
        self.assertTrue(stop_requested(world.store))
        self.assertEqual(run(world, op_take_over(world.store, COORDINATOR_B, COORDINATOR_A,
                                                 "pid_gone", 1)), [])
        self.assertEqual(indeterminate_records(world.store), [])
        self.assertTrue(store_root_fence_covers(world.store, "control/stop-requests/0001.json", 1))
        self.assertTrue(stop_requested(world.store))
        self.assertEqual(
            refused(world, op_reserve(ATTEMPT_B, COORDINATOR_B, OTHER_TASK, OTHER_SUBJECT,
                                      digest(DEFINITION))),
            "R-STOP-REQUESTED",
        )

    def test_a_completion_after_a_takeover_is_identical_not_a_conflict(self) -> None:
        """§7.2's `identical` has to be content identity: a successor re-derives the record under its
        own run and generation, so byte equality would report `conflict` for the very completions §8.2
        requires."""
        world = staged("reserve", "grant")
        self.assertEqual(run(world, op_prepare(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, None), 1), [])
        scope = copy.deepcopy(world.store.records[scope_path(ATTEMPT_A, "worker")])
        self.assertEqual(run(world, op_take_over(world.store, COORDINATOR_B, COORDINATOR_A,
                                                 "pid_gone", 1)), [])
        self.assertEqual(drain(world), [])
        self.assertEqual(world.store.conflicts, [])
        self.assertEqual(world.store.records[scope_path(ATTEMPT_A, "worker")], scope)
        self.assertTrue(world.store.has(f"attempts/{ATTEMPT_A}/prepared.json"))
        prepared = world.store.records[f"attempts/{ATTEMPT_A}/prepared.json"]
        self.assertEqual(prepared["coordinator"], COORDINATOR_B)
        self.assertEqual(prepared["written_in"], 1)


class RealValidatorTests(unittest.TestCase):
    """I4's schema half, dry-run through the shipped `check_candidate` rather than asserted here."""

    harness: RealValidatorHarness

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = RealValidatorHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    def test_an_accepted_task_commits_cleanly(self) -> None:
        world = staged(*ACCEPTED)
        self.assertEqual(self.harness.errors(build_candidate(world, self.harness.baseline)), [])

    def test_a_done_task_without_evidence_is_rejected_by_the_real_validator(self) -> None:
        world = staged(*READY)
        world.store.canonical[TASK] = {"status": "DONE", "evidence": []}
        errors = self.harness.errors(build_candidate(world, self.harness.baseline))
        self.assertTrue(any("DONE task requires evidence" in e for e in errors), errors)

    def test_r4_10_current_tasks_disagreeing_is_rejected_by_the_real_validator(self) -> None:
        world = staged(*ACCEPTED)
        candidate = build_candidate(world, self.harness.baseline)
        candidate["current_tasks"] = [TASK, OTHER_TASK]
        errors = self.harness.errors(candidate)
        self.assertTrue(any("does not match RUNNING tasks" in e for e in errors), errors)

    def test_a_forbidden_task_transition_is_rejected_by_the_real_validator(self) -> None:
        harness = RealValidatorHarness()
        try:
            world = staged(*ACCEPTED)
            candidate = build_candidate(world, harness.baseline)
            self.assertEqual(harness.errors(candidate), [])
            harness.commit(candidate)
            world.store.canonical[TASK] = {"status": "TODO", "evidence": []}
            errors = harness.errors(build_candidate(world, harness.baseline))
            self.assertTrue(any("DONE" in e for e in errors), errors)
        finally:
            harness.close()

    def test_the_receipt_half_of_i4_is_this_models_own_assertion(self) -> None:
        """`check_candidate` validates a v3 candidate's schema and transitions. It has no notion of a
        receipt, so an anchor that is not one passes it and fails I4 here."""
        world = staged(*READY)
        world.store.canonical[TASK] = {"status": "DONE", "evidence": ["not-a-receipt"]}
        self.assertEqual(self.harness.errors(build_candidate(world, self.harness.baseline)), [])
        self.assertTrue(any(v.startswith("I4") for v in violations(world, Start(ATTEMPT_A))))


if __name__ == "__main__":
    unittest.main()
