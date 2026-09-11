"""A small stateful reference model of the parallel-execution design (§21.2 of the reference).

Two coordinators, two tasks, one shared write path, one checker, one launch capability. Canonical
state and external execution are modelled **separately**: `Store` holds the durable records and the
canonical task state, `World` holds the modelled processes and the modelled bytes they write, and no
guard in this file reads a process to decide anything — guards read records, invariants read
processes. That separation is the whole point. Revision 4's suite derived "the worker is running"
from "the record says running", so every question it could ask had been answered by construction.

Recovery is a function from a store to a sequence of operations (`recover`) whose durable effects the
caller then applies, so recovery is exercised rather than asserted. Crashes are injected between
durable effects, at every prefix of every operation's sequence, by `crash_and_recover`.

The five invariants are stated externally, over the modelled world rather than over a label:

* **I1** no two executors hold conflicting claims — read off the registry of active grants.
* **I2** no claim is released while any modelled process can still write, including one whose
  coordinator is dead and whose launch capability is unconsumed.
* **I3** an acceptance's `accepted_snapshot` equals the modelled bytes at commit time and its
  `definition_hash` equals the modelled definition.
* **I4** a task is `DONE` only if its `evidence` holds the receipt — checked by building the real
  candidate and dry-running it through the real `check_candidate`.
* **I5** no projection reads a malformed or unexplainable record as absence: an unreadable record
  stops the reader instead of counting as nothing.

Every invariant is shown reachable-failure by `ForcedViolationTests`, which bypasses the guards and
requires the violation to appear. An invariant no scenario can break is not being checked.

**Passing this model does not establish that the protocol is correct.** There is no executor, no
filesystem and no adapter behind it; the world has one write path and one checker; and
`check_candidate` validates a candidate's schema and transitions, not a receipt's meaning, so the
receipt half of I4 is this file's own assertion. Eight of review 04's eighteen findings — R4-05,
R4-09, R4-11, R4-13, R4-15, R4-16, R4-17 and R4-18 — are not traces of this state machine and are not
modelled here: they are answered in the reference by §6 and §11.1, by refusal codes in §4, by this
replacement itself, by §17.1, and by the documentation checker and §21.3.
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

DEFINITION: "Dict[str, Dict[str, str]]" = {
    TASK: {"subject": SUBJECT, "check": "assert-nonempty"},
    OTHER_TASK: {"subject": OTHER_SUBJECT, "check": "assert-nonempty"},
}

# §12.2: the three proofs of death. Staleness and a different host are triggers, never proofs.
DEATH_PROOFS = frozenset({"host_rebooted", "pid_gone", "operator_attestation"})

# The fields each record kind must carry for a projection to read it at all (§7.6).
RECORD_FIELDS: "Dict[str, frozenset]" = {
    "reservation": frozenset({"kind", "attempt", "coordinator", "task", "subjects", "definition_hash"}),
    "grant": frozenset({"kind", "attempt", "coordinator", "task", "subjects", "definition_hash"}),
    "launch": frozenset({"kind", "attempt", "coordinator", "capability", "at"}),
    "result": frozenset({"kind", "attempt", "coordinator", "outcome", "code"}),
    "capture": frozenset({"kind", "attempt", "coordinator", "check", "subjects", "exit", "code"}),
    "acceptance": frozenset(
        {"kind", "attempt", "coordinator", "task", "accepted_snapshot", "definition_hash",
         "qualifying_captures", "intent", "receipt"}
    ),
    "release": frozenset({"kind", "attempt", "coordinator"}),
    "seal": frozenset({"kind", "attempt", "coordinator", "evidence"}),
    "classification": frozenset({"kind", "task", "coordinator", "cause_id", "cause_class"}),
    "resolution": frozenset({"kind", "task", "coordinator", "cause_id", "decided_at", "decided_by"}),
    "death-proof": frozenset({"kind", "coordinator", "proof", "taken_by", "successor_generation"}),
    "owner": frozenset({"kind", "coordinator", "generation"}),
}


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
    """§11.3: `rcp-` plus 32 hex over the five-key object. `receipt` itself is not one of the keys."""
    body = {
        "accepted_snapshot": accepted_snapshot,
        "attempt_id": attempt,
        "definition_hash": definition_hash,
        "intent": intent,
        "qualifying_captures": qualifying_captures,
    }
    return "rcp-" + digest(body)[:32]


@dataclass
class Adapter:
    """§17.1: what a host's executor can be evidenced to do, not what its documentation says."""

    name: str
    seals: bool          # a finished worker can be attested gone
    resumable: bool      # a finished worker can be restarted, so "finished" is not "sealed"


SEALING = Adapter("sealing", seals=True, resumable=False)
RESUMABLE = Adapter("resumable", seals=False, resumable=True)


@dataclass
class Store:
    """The durable side: publish-if-absent journal records, plus canonical task state."""

    records: "Dict[str, Any]" = field(default_factory=dict)
    canonical: "Dict[str, Dict[str, Any]]" = field(default_factory=dict)
    revision: int = 0
    conflicts: "List[str]" = field(default_factory=list)

    def publish(self, path: str, record: "Dict[str, Any]") -> str:
        """§7.2: never clobber. Identical bytes are success; different bytes are a recorded event."""
        existing = self.records.get(path)
        if existing is None:
            self.records[path] = copy.deepcopy(record)
            return "published"
        if existing == record:
            return "identical"
        self.conflicts.append(path)
        return "conflict"

    def of_kind(self, kind: str) -> "List[Dict[str, Any]]":
        return [r for r in self.records.values() if isinstance(r, dict) and r.get("kind") == kind]


@dataclass
class World:
    """Everything the store cannot see: the processes, the bytes, and the host's adapter."""

    adapter: Adapter = field(default_factory=lambda: SEALING)
    store: Store = field(default_factory=Store)
    armed: "Set[str]" = field(default_factory=set)     # launch authorized, not yet physically started
    running: "Set[str]" = field(default_factory=set)   # a process exists and can write
    content: "Dict[str, str]" = field(default_factory=dict)
    definition: "Dict[str, Dict[str, str]]" = field(default_factory=lambda: copy.deepcopy(DEFINITION))
    read_after_indeterminate: "List[str]" = field(default_factory=list)


def can_write(world: World, attempt: str) -> bool:
    """A launch that has been authorized but not yet fired can still write: that is R4-01."""
    return attempt in world.armed or attempt in world.running


# --------------------------------------------------------------------------------------------------
# Facts derived from records (§6.1) — never from a process
# --------------------------------------------------------------------------------------------------


def capability(store: Store, attempt: str) -> str:
    """`none → issued → consumed`. Consumption is the result record, so an unpublished result reads
    as `issued`, which is the conservative direction."""
    if f"launches/{attempt}.json" not in store.records:
        return "none"
    if f"results/{attempt}.json" in store.records:
        return "consumed"
    return "issued"


def sealed(store: Store, attempt: str) -> bool:
    return f"seals/{attempt}.json" in store.records


def active_grants(store: Store) -> "Dict[str, Dict[str, Any]]":
    grants = {}
    for record in store.of_kind("grant"):
        attempt = record["attempt"]
        if f"releases/{attempt}.json" not in store.records:
            grants[attempt] = record
    return grants


def open_causes(store: Store, task: str) -> "Set[str]":
    """§14.2: the gate closes on classification and opens on resolution. A stop is not a diagnosis."""
    resolved = {r["cause_id"] for r in store.of_kind("resolution")}
    return {
        r["cause_id"]
        for r in store.of_kind("classification")
        if r["task"] == task and r["cause_class"] != "operator_stop" and r["cause_id"] not in resolved
    }


def explained_generations(store: Store) -> "Set[int]":
    return {0} | {int(r["generation"]) for r in store.of_kind("owner")}


def projection(store: Store, path: str) -> str:
    """§6.3 and I5: `present`, `absent`, or `indeterminate`. A record that exists is never absent."""
    record = store.records.get(path)
    if record is None:
        return "absent"
    if not isinstance(record, dict):
        return "indeterminate"
    required = RECORD_FIELDS.get(record.get("kind"))
    if required is None or not required <= set(record):
        return "indeterminate"
    generation = record.get("generation")
    if generation is not None and int(generation) not in explained_generations(store):
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
class Arm:
    """The launch authorization taking physical effect. The spawn itself fires later, or never."""

    attempt: str


@dataclass(frozen=True)
class CommitCanonical:
    task: str
    receipt: str


Effect = Union[Publish, Arm, CommitCanonical]


@dataclass
class Operation:
    name: str
    code: str
    effects: "List[Effect]"


def apply_effect(world: World, effect: Effect) -> None:
    if indeterminate_records(world.store):
        world.read_after_indeterminate.append(type(effect).__name__)
    if isinstance(effect, Publish):
        world.store.publish(effect.path, effect.record)
    elif isinstance(effect, Arm):
        world.armed.add(effect.attempt)
    else:
        entry = world.store.canonical.setdefault(effect.task, {"status": "RUNNING", "evidence": []})
        entry["status"] = "DONE"
        if effect.receipt not in entry["evidence"]:
            entry["evidence"].append(effect.receipt)
        world.store.revision += 1


def op_reserve(attempt: str, coordinator: str, task: str, subject: str, definition_hash: str) -> Operation:
    return Operation("reserve", "O1", [
        Publish(f"claims/{attempt}.json", {
            "kind": "reservation", "attempt": attempt, "coordinator": coordinator,
            "task": task, "subjects": [subject], "definition_hash": definition_hash,
        }),
    ])


def op_grant(attempt: str, coordinator: str, task: str, subject: str, definition_hash: str) -> Operation:
    return Operation("grant", "O2", [
        Publish(f"grants/{attempt}.json", {
            "kind": "grant", "attempt": attempt, "coordinator": coordinator,
            "task": task, "subjects": [subject], "definition_hash": definition_hash,
        }),
    ])


def op_issue_capability(attempt: str, coordinator: str) -> Operation:
    """Two effects in one order, and R4-01 lives in the gap: the record is durable before the spawn."""
    return Operation("issue-capability", "O3", [
        Publish(f"launches/{attempt}.json", {
            "kind": "launch", "attempt": attempt, "coordinator": coordinator,
            "capability": "issued", "at": "2026-09-11T00:00:00+00:00",
        }),
        Arm(attempt),
    ])


def op_record_result(attempt: str, coordinator: str, outcome: str = "exited", code: int = 0) -> Operation:
    return Operation("record-result", "O10", [
        Publish(f"results/{attempt}.json", {
            "kind": "result", "attempt": attempt, "coordinator": coordinator,
            "outcome": outcome, "code": code,
        }),
    ])


def op_capture(attempt: str, coordinator: str, subject: str, after: "Optional[str]") -> Operation:
    return Operation("capture", "O8", [
        Publish(f"captures/{attempt}-0001.json", {
            "kind": "capture", "attempt": attempt, "coordinator": coordinator,
            "check": "assert-nonempty", "subjects": [{"path": subject, "after": after}],
            "exit": "exited", "code": 0,
        }),
    ])


def op_seal(attempt: str, coordinator: str, evidence: str) -> Operation:
    return Operation("seal", "O12", [
        Publish(f"seals/{attempt}.json", {
            "kind": "seal", "attempt": attempt, "coordinator": coordinator, "evidence": evidence,
        }),
    ])


def op_accept(
    attempt: str, coordinator: str, task: str,
    accepted_snapshot: "Dict[str, Optional[str]]", definition_hash: str,
) -> Operation:
    captures = [f"{attempt}-0001"]
    receipt = receipt_for(accepted_snapshot, attempt, definition_hash, "accept", captures)
    return Operation("accept", "O11", [
        Publish(f"acceptances/{attempt}.json", {
            "kind": "acceptance", "attempt": attempt, "coordinator": coordinator, "task": task,
            "accepted_snapshot": accepted_snapshot, "definition_hash": definition_hash,
            "qualifying_captures": captures, "intent": "accept", "receipt": receipt,
        }),
        CommitCanonical(task, receipt),
    ])


def op_withdraw(attempt: str, coordinator: str, task: str, cause: str) -> Operation:
    return Operation("withdraw", "O13", [
        Publish(f"withdrawals/{attempt}.json", {
            "kind": "classification", "task": task, "coordinator": coordinator,
            "cause_id": f"{attempt}-{cause}", "cause_class": cause,
        }),
    ])


def op_classify(task: str, coordinator: str, cause_id: str, cause_class: str) -> Operation:
    return Operation("classify", "O7", [
        Publish(f"causes/{cause_id}.json", {
            "kind": "classification", "task": task, "coordinator": coordinator,
            "cause_id": cause_id, "cause_class": cause_class,
        }),
    ])


def op_resolve(task: str, coordinator: str, cause_id: str) -> Operation:
    return Operation("resolve", "O9", [
        Publish(f"resolutions/{cause_id}.json", {
            "kind": "resolution", "task": task, "coordinator": coordinator, "cause_id": cause_id,
            "decided_at": "2026-09-11T01:00:00+00:00", "decided_by": "operator",
        }),
    ])


def op_release(attempt: str, coordinator: str) -> Operation:
    return Operation("release", "O16", [
        Publish(f"releases/{attempt}.json", {
            "kind": "release", "attempt": attempt, "coordinator": coordinator,
        }),
    ])


def op_take_over(coordinator: str, dead: str, proof: str, generation: int) -> Operation:
    """The proof names its successor, so a crash between the two records still has a completion.

    Without `taken_by` and `successor_generation` on the proof, a store holding only the first effect
    cannot say who was taking over, and `recover` would have to guess. That is the R4-04 shape: an
    ordered effect list is only recoverable if each effect determines the ones after it. The successor's
    generation is spelled separately from `generation`, which means "written by this generation" and is
    what `projection` checks: a proof announcing a generation is not yet a record written by one.
    """
    return Operation("take-over", "O17", [
        Publish(f"deaths/{dead}.json", {
            "kind": "death-proof", "coordinator": dead, "proof": proof,
            "taken_by": coordinator, "successor_generation": generation,
        }),
        Publish(f"owners/{coordinator}.json", {
            "kind": "owner", "coordinator": coordinator, "generation": generation,
        }),
    ])


# --------------------------------------------------------------------------------------------------
# Guards: refusal codes over facts (§4). Nothing here reads a process.
# --------------------------------------------------------------------------------------------------


def refuse_common(world: World) -> "Optional[str]":
    if indeterminate_records(world.store):
        return "R-INDETERMINATE"
    return None


def refuse_reserve(world: World, task: str, subject: str) -> "Optional[str]":
    if not world.adapter.seals:
        return "R-DETACHED-CHILD"
    if open_causes(world.store, task):
        return "R-GATE-CLOSED"
    for record in active_grants(world.store).values():
        if subject in record["subjects"]:
            return "R-CLAIM-CONFLICT"
    return None


def refuse_grant(world: World, attempt: str, subject: str) -> "Optional[str]":
    if f"claims/{attempt}.json" not in world.store.records:
        return "R-NO-RESERVATION"
    for other, record in active_grants(world.store).items():
        if other != attempt and subject in record["subjects"]:
            return "R-CLAIM-CONFLICT"
    return None


def refuse_issue_capability(world: World, attempt: str) -> "Optional[str]":
    if attempt not in active_grants(world.store):
        return "R-NO-GRANT"
    if capability(world.store, attempt) != "none":
        return "R-CAPABILITY-ISSUED"
    return None


def refuse_accept(
    world: World, attempt: str, task: str, snapshot_now: "Dict[str, Optional[str]]",
) -> "Optional[str]":
    grant = world.store.records.get(f"grants/{attempt}.json")
    if grant is None:
        return "R-NO-GRANT"
    capture = world.store.records.get(f"captures/{attempt}-0001.json")
    if capture is None:
        return "R-NO-CAPTURE"
    if digest(world.definition) != grant["definition_hash"]:
        return "definition_changed"
    for subject in capture["subjects"]:
        if snapshot_now.get(subject["path"]) != subject["after"]:
            return "stale_evidence"
    return None


def refuse_release(world: World, attempt: str) -> "Optional[str]":
    """I2's guard. `consumed` is not `sealed` when the adapter can resume a finished worker."""
    state = capability(world.store, attempt)
    if state == "issued" and not sealed(world.store, attempt):
        return "R-LAUNCH-IN-FLIGHT"
    if state == "consumed" and world.adapter.resumable and not sealed(world.store, attempt):
        return "R-LAUNCH-IN-FLIGHT"
    return None


def refuse_take_over(world: World, dead: str, proof: str) -> "Optional[str]":
    del dead
    if proof not in DEATH_PROOFS:
        return "R-NO-PROOF-OF-DEATH"
    return None


# --------------------------------------------------------------------------------------------------
# Recovery: a function from a store to the operations that complete every incomplete prefix (§8.2)
# --------------------------------------------------------------------------------------------------


def recover(store: Store) -> "List[Operation]":
    """Pure over the store. The caller applies the effects, so recovery is run rather than claimed.

    An issued-but-unconsumed capability has no completion: the attempt is indeterminate and its
    claims stay held, which is why a crash between `Publish` and `Arm` does not free the subject.
    """
    operations: "List[Operation]" = []
    for path in sorted(store.records):
        record = store.records[path]
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind == "reservation" and f"grants/{record['attempt']}.json" not in store.records:
            operations.append(op_grant(
                record["attempt"], record["coordinator"], record["task"],
                record["subjects"][0], record["definition_hash"],
            ))
        elif kind == "death-proof" and f"owners/{record['taken_by']}.json" not in store.records:
            operations.append(Operation("complete-take-over", "O17", [
                Publish(f"owners/{record['taken_by']}.json", {
                    "kind": "owner", "coordinator": record["taken_by"],
                    "generation": record["successor_generation"],
                }),
            ]))
        elif kind == "acceptance":
            committed = store.canonical.get(record["task"], {})
            if record["receipt"] not in committed.get("evidence", []):
                operations.append(Operation("complete-commit", "O11", [
                    CommitCanonical(record["task"], record["receipt"]),
                ]))
    return operations


# --------------------------------------------------------------------------------------------------
# The invariants, stated externally
# --------------------------------------------------------------------------------------------------


def violations(world: World, last: "Optional[Effect]" = None) -> "List[str]":
    store = world.store
    found: "List[str]" = []

    claimed: "Dict[str, str]" = {}
    for attempt, record in sorted(active_grants(store).items()):
        # A malformed grant is reported by I5, never dereferenced here: reading it as a claim
        # over nothing is exactly the absence-from-unreadability I5 exists to forbid.
        for subject in record.get("subjects") or []:
            if subject in claimed and claimed[subject] != attempt:
                found.append(f"I1: {claimed[subject]} and {attempt} both hold {subject}")
            claimed[subject] = attempt

    for record in store.of_kind("release"):
        if can_write(world, record["attempt"]):
            found.append(f"I2: {record['attempt']} is released while a modelled process can write")

    if isinstance(last, Publish) and last.record.get("kind") == "acceptance":
        subjects = sorted(last.record["accepted_snapshot"])
        if last.record["accepted_snapshot"] != snapshot_of(world, subjects):
            found.append("I3: accepted_snapshot is not the modelled bytes at commit time")
        if last.record["definition_hash"] != digest(world.definition):
            found.append("I3: definition_hash is not the modelled definition")

    for task, entry in sorted(store.canonical.items()):
        if entry.get("status") == "DONE" and not entry.get("evidence"):
            found.append(f"I4: {task} is DONE with no receipt in its evidence")

    for path in store.records:
        if projection(store, path) == "absent":
            found.append(f"I5: {path} projects as absent while its record exists")
    if world.read_after_indeterminate:
        found.append("I5: an effect was applied while the store held an unreadable record")
    return found


# --------------------------------------------------------------------------------------------------
# Staging, crash injection, and the scenario table
# --------------------------------------------------------------------------------------------------

DEFINITION_HASH = digest(DEFINITION)


def run(world: World, operation: Operation, prefix: "Optional[int]" = None) -> "List[str]":
    limit = len(operation.effects) if prefix is None else prefix
    found: "List[str]" = []
    for effect in operation.effects[:limit]:
        apply_effect(world, effect)
        found.extend(violations(world, effect))
    return found


def drain(world: World, rounds: int = 8) -> "List[str]":
    found: "List[str]" = []
    for _ in range(rounds):
        pending = recover(world.store)
        if not pending:
            return found
        for operation in pending:
            found.extend(run(world, operation))
    found.append("recovery did not reach a fixpoint")
    return found


def staged(*stages: str, adapter: Adapter = SEALING) -> World:
    """Build a world at a named point in one attempt's life. External steps are explicit."""
    world = World(adapter=adapter)
    for stage in stages:
        if stage == "reserve":
            run(world, op_reserve(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, DEFINITION_HASH))
        elif stage == "grant":
            run(world, op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, DEFINITION_HASH))
        elif stage == "capability":
            run(world, op_issue_capability(ATTEMPT_A, COORDINATOR_A))
        elif stage == "fire":
            world.armed.discard(ATTEMPT_A)
            world.running.add(ATTEMPT_A)
        elif stage == "write":
            world.content[SUBJECT] = "produced"
        elif stage == "exit":
            world.running.discard(ATTEMPT_A)
        elif stage == "result":
            run(world, op_record_result(ATTEMPT_A, COORDINATOR_A))
        elif stage == "capture":
            run(world, op_capture(ATTEMPT_A, COORDINATOR_A, SUBJECT, digest(world.content[SUBJECT])))
        elif stage == "seal":
            run(world, op_seal(ATTEMPT_A, COORDINATOR_A, "pid_gone"))
        else:  # pragma: no cover - a typo in a scenario name must not pass silently
            raise AssertionError(f"unknown stage {stage!r}")
    return world


READY = ("reserve", "grant", "capability", "fire", "write", "exit", "result", "capture", "seal")

# name -> (the stages that must already have happened, the operation whose prefixes are injected)
SCENARIOS: "Tuple[Tuple[str, Tuple[str, ...], Callable[[World], Operation]], ...]" = (
    ("O1 reserve", (), lambda w: op_reserve(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, DEFINITION_HASH)),
    ("O2 grant", ("reserve",), lambda w: op_grant(ATTEMPT_A, COORDINATOR_A, TASK, SUBJECT, DEFINITION_HASH)),
    ("O3 issue-capability", ("reserve", "grant"), lambda w: op_issue_capability(ATTEMPT_A, COORDINATOR_A)),
    ("O10 record-result", ("reserve", "grant", "capability", "fire", "write", "exit"),
     lambda w: op_record_result(ATTEMPT_A, COORDINATOR_A)),
    ("O8 capture", ("reserve", "grant", "capability", "fire", "write", "exit", "result"),
     lambda w: op_capture(ATTEMPT_A, COORDINATOR_A, SUBJECT, digest(w.content[SUBJECT]))),
    ("O11 accept", READY,
     lambda w: op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot_of(w, [SUBJECT]), DEFINITION_HASH)),
    ("O16 release", (*READY, "accept"), lambda w: op_release(ATTEMPT_A, COORDINATOR_A)),
    ("O7 classify", ("reserve", "grant"),
     lambda w: op_classify(TASK, COORDINATOR_A, "c-1", "adapter_error")),
    ("O9 resolve", ("reserve", "grant"), lambda w: op_resolve(TASK, COORDINATOR_A, "c-1")),
    ("O17 take-over", ("reserve", "grant", "capability"),
     lambda w: op_take_over(COORDINATOR_B, COORDINATOR_A, "pid_gone", 1)),
)


def build(stages: "Tuple[str, ...]") -> World:
    if stages and stages[-1] == "accept":
        world = staged(*stages[:-1])
        run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot_of(world, [SUBJECT]), DEFINITION_HASH))
        return world
    return staged(*stages)


# --------------------------------------------------------------------------------------------------
# I4: the real candidate, dry-run through the real validator
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
    """§8.2: every prefix of every operation has exactly one completion, and no prefix violates
    an invariant on the way there."""

    def test_no_prefix_violates_an_invariant(self) -> None:
        for name, stages, builder in SCENARIOS:
            for prefix in range(len(builder(build(stages)).effects) + 1):
                with self.subTest(operation=name, prefix=prefix):
                    world = build(stages)
                    found = run(world, builder(world), prefix)
                    found.extend(drain(world))
                    self.assertEqual([], found)

    def test_every_prefix_recovers_to_the_uncrashed_store(self) -> None:
        for name, stages, builder in SCENARIOS:
            complete = build(stages)
            run(complete, builder(complete))
            drain(complete)
            expected = (complete.store.records, complete.store.canonical)
            for prefix in range(len(builder(build(stages)).effects) + 1):
                with self.subTest(operation=name, prefix=prefix):
                    world = build(stages)
                    run(world, builder(world), prefix)
                    drain(world)
                    if prefix == 0:
                        continue
                    self.assertEqual(expected, (world.store.records, world.store.canonical))

    def test_recovery_of_a_complete_operation_is_a_no_op(self) -> None:
        for name, stages, builder in SCENARIOS:
            with self.subTest(operation=name):
                world = build(stages)
                run(world, builder(world))
                drain(world)
                self.assertEqual([], recover(world.store))

    def test_a_crash_before_the_spawn_does_not_free_the_subject(self) -> None:
        """R4-01's prefix: the launch record is durable, the spawn is not, and recovery must not
        conclude that nothing happened."""
        world = staged("reserve", "grant")
        run(world, op_issue_capability(ATTEMPT_A, COORDINATOR_A), prefix=1)
        drain(world)
        self.assertEqual("issued", capability(world.store, ATTEMPT_A))
        self.assertIn(ATTEMPT_A, active_grants(world.store))
        self.assertEqual("R-LAUNCH-IN-FLIGHT", refuse_release(world, ATTEMPT_A))


class ForcedViolationTests(unittest.TestCase):
    """Each invariant must be breakable with the guards bypassed, or it is not being checked."""

    def test_i1_fires_when_two_grants_cover_one_subject(self) -> None:
        world = staged("reserve", "grant")
        run(world, op_grant(ATTEMPT_B, COORDINATOR_B, OTHER_TASK, SUBJECT, DEFINITION_HASH))
        self.assertTrue(any(v.startswith("I1") for v in violations(world)))

    def test_i2_fires_when_an_armed_launch_is_released(self) -> None:
        world = staged("reserve", "grant", "capability")
        self.assertEqual("R-LAUNCH-IN-FLIGHT", refuse_release(world, ATTEMPT_A))
        run(world, op_release(ATTEMPT_A, COORDINATOR_A))
        self.assertTrue(any(v.startswith("I2") for v in violations(world)))

    def test_i3_fires_when_the_bytes_moved_under_the_acceptance(self) -> None:
        world = staged(*READY)
        stale = snapshot_of(world, [SUBJECT])
        world.content[SUBJECT] = "rewritten after the check ran"
        found = run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, stale, DEFINITION_HASH))
        self.assertTrue(any(v.startswith("I3") for v in found))

    def test_i3_fires_when_the_definition_moved_under_the_acceptance(self) -> None:
        world = staged(*READY)
        snapshot = snapshot_of(world, [SUBJECT])
        world.definition[TASK]["check"] = "assert-two-lines"
        found = run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot, DEFINITION_HASH))
        self.assertTrue(any("definition_hash" in v for v in found))

    def test_i4_fires_when_a_task_is_done_without_a_receipt(self) -> None:
        world = staged(*READY)
        world.store.canonical[TASK] = {"status": "DONE", "evidence": []}
        self.assertTrue(any(v.startswith("I4") for v in violations(world)))

    def test_i5_fires_when_an_effect_is_applied_over_an_unreadable_record(self) -> None:
        world = staged("reserve", "grant")
        world.store.records["grants/att-c.json"] = {"kind": "grant", "attempt": ATTEMPT_B}
        self.assertEqual("R-INDETERMINATE", refuse_common(world))
        run(world, op_issue_capability(ATTEMPT_A, COORDINATOR_A))
        self.assertTrue(any(v.startswith("I5") for v in violations(world)))


class ProjectionTests(unittest.TestCase):
    """I5: an unreadable record stops the reader. Absence is only ever a missing record."""

    def test_a_missing_record_is_absent(self) -> None:
        self.assertEqual("absent", projection(Store(), "grants/att-a.json"))

    def test_a_well_formed_record_is_present(self) -> None:
        world = staged("reserve", "grant")
        self.assertEqual("present", projection(world.store, f"grants/{ATTEMPT_A}.json"))

    def test_a_record_missing_a_required_field_is_indeterminate(self) -> None:
        store = Store(records={"grants/x.json": {"kind": "grant", "attempt": "x"}})
        self.assertEqual("indeterminate", projection(store, "grants/x.json"))

    def test_an_unknown_kind_is_indeterminate(self) -> None:
        store = Store(records={"grants/x.json": {"kind": "vote", "attempt": "x"}})
        self.assertEqual("indeterminate", projection(store, "grants/x.json"))

    def test_a_non_object_record_is_indeterminate(self) -> None:
        store = Store(records={"grants/x.json": ["not", "an", "object"]})
        self.assertEqual("indeterminate", projection(store, "grants/x.json"))

    def test_an_unexplained_generation_is_indeterminate(self) -> None:
        """R4-13's shape: a record from a writer this store never recorded taking over."""
        world = staged("reserve", "grant")
        record = dict(world.store.records[f"grants/{ATTEMPT_A}.json"])
        record["generation"] = 7
        world.store.records["grants/att-z.json"] = record
        self.assertEqual("indeterminate", projection(world.store, "grants/att-z.json"))
        run(world, op_take_over(COORDINATOR_B, COORDINATOR_A, "pid_gone", 7))
        self.assertEqual("present", projection(world.store, "grants/att-z.json"))


class GuardTests(unittest.TestCase):
    """Every refusal code is returned by the situation §4 says it names."""

    def test_a_resumable_adapter_is_refused_at_admission(self) -> None:
        world = World(adapter=RESUMABLE)
        self.assertEqual("R-DETACHED-CHILD", refuse_reserve(world, TASK, SUBJECT))

    def test_a_held_subject_refuses_a_second_reservation(self) -> None:
        world = staged("reserve", "grant")
        self.assertEqual("R-CLAIM-CONFLICT", refuse_reserve(world, OTHER_TASK, SUBJECT))

    def test_a_grant_without_a_reservation_is_refused(self) -> None:
        self.assertEqual("R-NO-RESERVATION", refuse_grant(World(), ATTEMPT_A, SUBJECT))

    def test_a_second_capability_is_refused(self) -> None:
        world = staged("reserve", "grant", "capability")
        self.assertEqual("R-CAPABILITY-ISSUED", refuse_issue_capability(world, ATTEMPT_A))

    def test_a_capability_without_a_grant_is_refused(self) -> None:
        world = staged("reserve")
        self.assertEqual("R-NO-GRANT", refuse_issue_capability(world, ATTEMPT_A))

    def test_takeover_without_a_proof_of_death_is_refused(self) -> None:
        world = staged("reserve", "grant", "capability")
        self.assertEqual("R-NO-PROOF-OF-DEATH", refuse_take_over(world, COORDINATOR_A, "stale_heartbeat"))
        self.assertEqual("R-NO-PROOF-OF-DEATH", refuse_take_over(world, COORDINATOR_A, "different_host"))
        for proof in sorted(DEATH_PROOFS):
            self.assertIsNone(refuse_take_over(world, COORDINATOR_A, proof))

    def test_a_ready_attempt_accepts(self) -> None:
        world = staged(*READY)
        self.assertIsNone(refuse_accept(world, ATTEMPT_A, TASK, snapshot_of(world, [SUBJECT])))


class ReviewFourRegressionTests(unittest.TestCase):
    """One named case per trace in docs/parallel-execution-review-04.md."""

    def test_r4_01_a_delayed_launch_cannot_outlive_its_reservation(self) -> None:
        """The record says issued, the coordinator dies, the spawn fires afterwards."""
        world = staged("reserve", "grant", "capability")
        self.assertEqual("R-NO-PROOF-OF-DEATH", refuse_take_over(world, COORDINATOR_A, "stale_heartbeat"))
        run(world, op_take_over(COORDINATOR_B, COORDINATOR_A, "pid_gone", 1))
        self.assertEqual("R-LAUNCH-IN-FLIGHT", refuse_release(world, ATTEMPT_A))
        # Forced anyway, the delayed spawn is exactly the violation the guard exists to prevent.
        run(world, op_release(ATTEMPT_A, COORDINATOR_A))
        world.armed.discard(ATTEMPT_A)
        world.running.add(ATTEMPT_A)
        self.assertTrue(any(v.startswith("I2") for v in violations(world)))

    def test_r4_02_a_superseded_coordinator_cannot_clobber_a_journal_record(self) -> None:
        world = staged("reserve", "grant", "capability", "fire", "write", "exit")
        run(world, op_take_over(COORDINATOR_B, COORDINATOR_A, "pid_gone", 1))
        run(world, op_record_result(ATTEMPT_A, COORDINATOR_B))
        before = copy.deepcopy(world.store.records[f"results/{ATTEMPT_A}.json"])
        run(world, op_record_result(ATTEMPT_A, COORDINATOR_A, outcome="signalled", code=9))
        self.assertEqual(before, world.store.records[f"results/{ATTEMPT_A}.json"])
        self.assertEqual([f"results/{ATTEMPT_A}.json"], world.store.conflicts)

    def test_r4_03_a_finished_worker_is_not_a_sealed_one(self) -> None:
        """The review's point, made worse by this host's own adapter: completed is resumable."""
        world = staged("reserve", "grant", "capability", "fire", "write", "exit", "result")
        world.adapter = RESUMABLE
        self.assertEqual("R-LAUNCH-IN-FLIGHT", refuse_release(world, ATTEMPT_A))
        run(world, op_release(ATTEMPT_A, COORDINATOR_A))
        world.running.add(ATTEMPT_A)  # the adapter resumed a "finished" worker
        self.assertTrue(any(v.startswith("I2") for v in violations(world)))

    def test_r4_03_sealing_is_what_makes_a_release_safe(self) -> None:
        world = staged("reserve", "grant", "capability", "fire", "write", "exit", "result", "seal")
        world.adapter = RESUMABLE
        self.assertIsNone(refuse_release(world, ATTEMPT_A))
        run(world, op_release(ATTEMPT_A, COORDINATOR_A))
        self.assertEqual([], violations(world))

    def test_r4_04_the_acquisition_orphan_prefix_completes(self) -> None:
        """A reservation with no grant is completed by recovery, not leaked and not re-reserved."""
        world = staged("reserve")
        self.assertNotIn(ATTEMPT_A, active_grants(world.store))
        drain(world)
        self.assertIn(ATTEMPT_A, active_grants(world.store))
        self.assertEqual([], recover(world.store))

    def test_r4_06_the_dispatch_gate_opens_on_resolution_not_classification(self) -> None:
        world = staged("reserve", "grant", "capability", "fire", "exit")
        run(world, op_classify(TASK, COORDINATOR_A, "c-1", "adapter_error"))
        self.assertEqual({"c-1"}, open_causes(world.store, TASK))
        self.assertEqual("R-GATE-CLOSED", refuse_reserve(world, TASK, OTHER_SUBJECT))
        run(world, op_resolve(TASK, COORDINATOR_A, "c-1"))
        self.assertEqual(set(), open_causes(world.store, TASK))
        self.assertIsNone(refuse_reserve(world, TASK, OTHER_SUBJECT))

    def test_r4_06_an_operator_stop_does_not_close_the_gate(self) -> None:
        world = staged("reserve", "grant")
        run(world, op_classify(TASK, COORDINATOR_A, "c-2", "operator_stop"))
        self.assertEqual(set(), open_causes(world.store, TASK))

    def test_r4_07_evidence_is_bound_to_the_accepted_bytes(self) -> None:
        world = staged(*READY)
        world.content[SUBJECT] = "rewritten after the check ran"
        self.assertEqual(
            "stale_evidence", refuse_accept(world, ATTEMPT_A, TASK, snapshot_of(world, [SUBJECT]))
        )

    def test_r4_08_a_changed_definition_withdraws_instead_of_accepting(self) -> None:
        world = staged(*READY)
        world.definition[TASK]["check"] = "assert-two-lines"
        self.assertEqual(
            "definition_changed",
            refuse_accept(world, ATTEMPT_A, TASK, snapshot_of(world, [SUBJECT])),
        )
        run(world, op_withdraw(ATTEMPT_A, COORDINATOR_A, TASK, "definition_changed"))
        self.assertEqual({f"{ATTEMPT_A}-definition_changed"}, open_causes(world.store, TASK))

    def test_r4_12_identical_bytes_are_success_and_different_bytes_are_an_event(self) -> None:
        store = Store()
        record = {"kind": "release", "attempt": ATTEMPT_A, "coordinator": COORDINATOR_A}
        self.assertEqual("published", store.publish("releases/a.json", record))
        self.assertEqual("identical", store.publish("releases/a.json", dict(record)))
        self.assertEqual([], store.conflicts)
        other = dict(record, coordinator=COORDINATOR_B)
        self.assertEqual("conflict", store.publish("releases/a.json", other))
        self.assertEqual(["releases/a.json"], store.conflicts)
        self.assertEqual(record, store.records["releases/a.json"])

    def test_r4_14_a_commit_is_proved_by_the_receipt_not_by_a_status(self) -> None:
        world = staged(*READY)
        snapshot = snapshot_of(world, [SUBJECT])
        run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot, DEFINITION_HASH))
        expected = receipt_for(snapshot, ATTEMPT_A, DEFINITION_HASH, "accept", [f"{ATTEMPT_A}-0001"])
        self.assertEqual([expected], world.store.canonical[TASK]["evidence"])
        # A status with no receipt behind it is not a landed commit, whatever the status says.
        world.store.canonical[OTHER_TASK] = {"status": "DONE", "evidence": []}
        self.assertTrue(any(v.startswith("I4") for v in violations(world)))


class RealValidatorTests(unittest.TestCase):
    """I4 through the shipped code: the candidate is built for real and dry-run for real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = RealValidatorHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    def test_an_accepted_task_commits_cleanly(self) -> None:
        world = staged(*READY)
        run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot_of(world, [SUBJECT]), DEFINITION_HASH))
        self.assertEqual([], self.harness.errors(build_candidate(world, self.harness.baseline)))

    def test_a_done_task_without_evidence_is_rejected_by_the_real_validator(self) -> None:
        world = staged(*READY)
        world.store.canonical[TASK] = {"status": "DONE", "evidence": []}
        errors = self.harness.errors(build_candidate(world, self.harness.baseline))
        self.assertTrue(any("DONE task requires evidence" in e for e in errors), errors)

    def test_r4_10_current_tasks_disagreeing_is_rejected_by_the_real_validator(self) -> None:
        """R4-10: revision 4's mutations were asserted legal without ever being validated."""
        world = staged(*READY)
        candidate = build_candidate(world, self.harness.baseline)
        candidate["current_tasks"] = [TASK]
        errors = self.harness.errors(candidate)
        self.assertTrue(any("does not match RUNNING tasks" in e for e in errors), errors)

    def test_a_forbidden_task_transition_is_rejected_by_the_real_validator(self) -> None:
        """`DONE` is terminal, so a rerun cannot be modelled as walking a task back."""
        harness = RealValidatorHarness()
        self.addCleanup(harness.close)
        world = staged(*READY)
        run(world, op_accept(ATTEMPT_A, COORDINATOR_A, TASK, snapshot_of(world, [SUBJECT]), DEFINITION_HASH))
        clean = build_candidate(world, harness.baseline)
        self.assertEqual([], harness.errors(clean))
        harness.commit(clean)
        world.store.canonical[TASK] = {"status": "TODO", "evidence": []}
        errors = harness.errors(build_candidate(world, harness.baseline))
        self.assertTrue(any("DONE" in e for e in errors), errors)

    def test_the_receipt_half_of_i4_is_this_models_own_assertion(self) -> None:
        """The validator checks that evidence exists, not that its anchor is a receipt. Saying so
        here keeps a green run from being read as more than it is."""
        world = staged(*READY)
        world.store.canonical[TASK] = {"status": "DONE", "evidence": ["not-a-receipt"]}
        self.assertEqual([], self.harness.errors(build_candidate(world, self.harness.baseline)))
        self.assertFalse(
            world.store.canonical[TASK]["evidence"][0].startswith("rcp-"),
            "the model, not the validator, is what rejects this",
        )


if __name__ == "__main__":
    unittest.main()
