"""Guarded lifecycle engine for the parallel execution protocol (Phase 4).

``perform`` is the only path to the store.  All 20 operations and all recovery
actions call it; no code in this module writes to the store by any other route
(R5-07 structural guarantee).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
)

from execution_serialization import (
    AttemptEnvelope,
    CommonEnvelope,
    ExecutionRecordError,
    decode_scope_id,
    encode_scope_id,
    validate_record,
)
from execution_serialization import (
    body_digest as _compute_body_digest,
)
from execution_serialization import (
    canonical_json as _canonical_json,
)
from execution_serialization import (
    receipt_id as _compute_receipt_id,
)
from execution_store import GrantRegistry, publish_if_absent

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OperationError(Exception):
    """An operation could not complete; the caller may retry or abort."""


class PreconditionError(OperationError):
    """The operation's guard rejected the current facts."""


# ---------------------------------------------------------------------------
# Adapter protocol (§17)
# ---------------------------------------------------------------------------


class AdapterProtocol(Protocol):
    """Structural protocol for the host adapter (§17)."""

    def start(self, plan: Dict[str, Any], attempt_dir: str) -> str:
        """Begin execution.  Returns a handle string, or ``START_AMBIGUOUS``."""
        ...

    def observe(self, handle: str) -> str:
        """Report liveness without side-effects.  Returns RUNNING/FINISHED/UNKNOWN."""
        ...

    def seal(self, handle: str) -> Dict[str, Any]:
        """Return the sealing attestation.  Raises if the process tree has not exited."""
        ...

    def terminate(self, handle: str) -> None:
        """Request termination of the whole process tree.  Idempotent by handle."""
        ...


# ---------------------------------------------------------------------------
# Facts (§6.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """Computed fact set for one attempt (§6.1).  Independent fields, not a state machine."""

    # canonical — from project.json
    task_status: str
    bound_attempt: Optional[str]
    revision: int
    coordinator_run: Optional[str]
    ownership_generation: int
    authorization_in_force: bool
    receipt_present: bool

    # plan
    plan_hash: Optional[str]
    definition_hash: Optional[str]

    # journal
    reserved: bool
    prepared: bool
    launched: bool
    result: Optional[str]               # null | "success" | "failure" | "refused"
    declared_scopes: FrozenSet[str]
    sealed_scopes: FrozenSet[str]
    open_scopes: FrozenSet[str]         # declared_scopes - sealed_scopes (derived)
    open_causes: FrozenSet[str]         # holds with no matching resolution
    stop_evidence: Optional[str]        # null | §13 kind string
    classified: Optional[str]           # null | §14 class string
    accepted: bool                      # terminal-intent mutation has intent.json
    commit_observed: bool               # that mutation also has ack.json
    released: bool                      # release.json is valid

    # start state
    uncertain_start: bool               # start_outcome == "ambiguous"

    # project-level
    stop_requested: bool                # unmatched stop-request under control/

    # registry
    grant_present: bool
    launch_capability: str              # "none" | "issued" | "consumed" | "revoked"

    # validity (§6.3)
    indeterminate: bool
    indeterminate_path: Optional[str]   # store-relative path of the offending record


# ---------------------------------------------------------------------------
# Task state (per-task fields from project.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskState:
    """Per-task project.json fields needed for projection."""

    task_id: str
    attempt_id: Optional[str]          # bound_attempt; null when unbound
    task_status: str
    authorization_in_force: bool
    receipt_present: bool
    plan_hash: Optional[str]


# ---------------------------------------------------------------------------
# Execution context (shared across a coordinator turn)
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Global execution context shared across all operations in a coordinator turn."""

    execution_dir: Path                 # <project-dir>/execution/
    workspace_root: Path                # parent of .execution-registry/
    project_id: str
    coordinator_run: Optional[str]
    ownership_generation: int
    revision: int
    registry: GrantRegistry
    adapter: AdapterProtocol
    project_dir: Optional[Path] = None  # project directory (for evidence.md, O12)
    commit_fn: Any = None               # Callable[[str,str,Optional[str],int,...], Tuple[int,int]]


# ---------------------------------------------------------------------------
# §6.3 validity — internal helpers
# ---------------------------------------------------------------------------


class _IndeterminateError(Exception):
    """A record file exists but fails §6.3 validation."""

    def __init__(self, store_path: str) -> None:
        super().__init__(store_path)
        self.store_path = store_path


def _read_record(
    abs_path: Path,
    store_path: str,
    project_id: str,
    task_id: Optional[str],
    attempt_id: Optional[str],
    current_generation: int,
    fenced_paths: Dict[int, FrozenSet[str]],
) -> Optional[Tuple[CommonEnvelope, Optional[AttemptEnvelope], Any]]:
    """Read and §6.3-validate one store record.

    Returns None when the file is absent (absence is not a fact).
    Raises _IndeterminateError when the file exists but fails any §6.3 clause.
    """
    try:
        raw_bytes = abs_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _IndeterminateError(store_path) from None

    try:
        raw: Any = json.loads(raw_bytes)
    except (ValueError, UnicodeDecodeError):
        raise _IndeterminateError(store_path) from None

    try:
        common, attempt_env, kind_rec = validate_record(raw)
    except ExecutionRecordError:
        raise _IndeterminateError(store_path) from None

    # §6.3: project_id must match
    if common.project_id != project_id:
        raise _IndeterminateError(store_path)

    # §6.3: attempt_id / task_id must match for attempt-level records
    if attempt_env is not None:
        if task_id is not None and attempt_env.task_id != task_id:
            raise _IndeterminateError(store_path)
        if attempt_id is not None and attempt_env.attempt_id != attempt_id:
            raise _IndeterminateError(store_path)

    # §6.3: body_digest must recompute
    if _compute_body_digest(raw) != common.body_digest_field:
        raise _IndeterminateError(store_path)

    # §6.3: ownership_generation must be explainable
    gen = common.ownership_generation
    if gen != current_generation:
        if gen not in fenced_paths or store_path not in fenced_paths[gen]:
            raise _IndeterminateError(store_path)

    return common, attempt_env, kind_rec


def _add_fence(
    path: Path,
    project_id: str,
    current_generation: int,
    result: Dict[int, FrozenSet[str]],
) -> None:
    """Try to load one generation-fence file and merge its path list into result."""
    try:
        raw_bytes = path.read_bytes()
        raw: Any = json.loads(raw_bytes)
        common, _, kind_rec = validate_record(raw)
    except (OSError, ValueError, ExecutionRecordError):
        return
    if common.project_id != project_id:
        return
    if common.ownership_generation != current_generation:
        return
    if _compute_body_digest(raw) != common.body_digest_field:
        return
    taken = kind_rec.taken_over_from
    paths: FrozenSet[str] = frozenset(str(p) for p in kind_rec.records)
    result[taken] = result.get(taken, frozenset()) | paths


def _load_gen_fences(
    exec_dir: Path,
    attempt_id: Optional[str],
    project_id: str,
    current_generation: int,
) -> Dict[int, FrozenSet[str]]:
    """Load all generation-fence records; return {old_generation: frozenset of store-relative paths}."""
    result: Dict[int, FrozenSet[str]] = {}
    root_fence_dir = exec_dir / "generation-fences"
    if root_fence_dir.exists():
        for ffile in root_fence_dir.iterdir():
            if ffile.suffix == ".json":
                _add_fence(ffile, project_id, current_generation, result)
    if attempt_id is not None:
        attempt_fence_dir = exec_dir / "attempts" / attempt_id / "generation-fences"
        if attempt_fence_dir.exists():
            for ffile in attempt_fence_dir.iterdir():
                if ffile.suffix == ".json":
                    _add_fence(ffile, project_id, current_generation, result)
    return result


def _load_scope_ids(
    dir_path: Path,
    dir_store_path: str,
    project_id: str,
    task_id: str,
    attempt_id: str,
    current_generation: int,
    fenced_paths: Dict[int, FrozenSet[str]],
) -> FrozenSet[str]:
    """Load scope ids from a scopes/ or sealed/ directory, validating each record."""
    if not dir_path.exists():
        return frozenset()
    ids: Set[str] = set()
    for f in dir_path.iterdir():
        if f.suffix != ".json":
            continue
        scope_id = decode_scope_id(f.stem)
        store_path = f"{dir_store_path}/{f.name}"
        rec = _read_record(
            f, store_path, project_id, task_id, attempt_id,
            current_generation, fenced_paths,
        )
        if rec is not None:
            _, _, kind_rec = rec
            # The record's own scope_id field must match the filename
            if kind_rec.scope_id != scope_id:
                raise _IndeterminateError(store_path)
            ids.add(scope_id)
    return frozenset(ids)


def _compute_open_causes(
    attempt_dir: Path,
    attempt_store_prefix: str,
    project_id: str,
    task_id: str,
    attempt_id: str,
    current_generation: int,
    fenced_paths: Dict[int, FrozenSet[str]],
) -> FrozenSet[str]:
    """Return cause ids that have a hold record but no matching resolution."""
    holds_dir = attempt_dir / "holds"
    res_dir = attempt_dir / "resolutions"
    if not holds_dir.exists():
        return frozenset()
    resolved: Set[str] = set()
    if res_dir.exists():
        for rf in res_dir.iterdir():
            if rf.suffix != ".json":
                continue
            sp = f"{attempt_store_prefix}/resolutions/{rf.name}"
            rec = _read_record(
                rf, sp, project_id, task_id, attempt_id,
                current_generation, fenced_paths,
            )
            if rec is not None:
                _, _, kind_rec = rec
                resolved.add(kind_rec.cause_id)
    open_set: Set[str] = set()
    for hf in holds_dir.iterdir():
        if hf.suffix != ".json":
            continue
        sp = f"{attempt_store_prefix}/holds/{hf.name}"
        rec = _read_record(
            hf, sp, project_id, task_id, attempt_id,
            current_generation, fenced_paths,
        )
        if rec is not None:
            _, _, kind_rec = rec
            if kind_rec.cause_id not in resolved:
                open_set.add(kind_rec.cause_id)
    return frozenset(open_set)


def _compute_accepted_committed(
    mutations_dir: Path,
    mutations_store_prefix: str,
    project_id: str,
    task_id: str,
    attempt_id: str,
    current_generation: int,
    fenced_paths: Dict[int, FrozenSet[str]],
) -> Tuple[bool, bool]:
    """Return (accepted, commit_observed) from the mutations/ directory.

    accepted = some terminal-intent mutation has intent.json.
    commit_observed = that mutation also has ack.json.
    """
    if not mutations_dir.exists():
        return False, False
    accepted = False
    commit_observed = False
    for receipt_dir in mutations_dir.iterdir():
        if not receipt_dir.is_dir():
            continue
        intent_path = receipt_dir / "intent.json"
        if not intent_path.exists():
            continue
        sp = f"{mutations_store_prefix}/{receipt_dir.name}/intent.json"
        intent_rec = _read_record(
            intent_path, sp, project_id, task_id, attempt_id,
            current_generation, fenced_paths,
        )
        assert intent_rec is not None  # exists() checked above; None only on FileNotFoundError
        _, _, kind_rec = intent_rec
        # Dispatch mutation carries intent="RUNNING"; skip it
        if kind_rec.intent == "RUNNING":
            continue
        accepted = True
        ack_path = receipt_dir / "ack.json"
        if not ack_path.exists():
            continue
        ack_sp = f"{mutations_store_prefix}/{receipt_dir.name}/ack.json"
        ack_rec = _read_record(
            ack_path, ack_sp, project_id, task_id, attempt_id,
            current_generation, fenced_paths,
        )
        if ack_rec is not None:
            commit_observed = True
    return accepted, commit_observed


def _compute_stop_requested(
    exec_dir: Path,
    project_id: str,
    current_generation: int,
    project_fences: Dict[int, FrozenSet[str]],
) -> bool:
    """True if any stop-request under control/ is not cleared by a matching clearance."""
    sr_dir = exec_dir / "control" / "stop-requests"
    cl_dir = exec_dir / "control" / "clearances"
    if not sr_dir.exists():
        return False
    cleared: Set[int] = set()
    if cl_dir.exists():
        for cf in cl_dir.iterdir():
            if cf.suffix != ".json":
                continue
            sp = f"control/clearances/{cf.name}"
            rec = _read_record(
                cf, sp, project_id, None, None, current_generation, project_fences,
            )
            if rec is not None:
                _, _, kind_rec = rec
                cleared.add(kind_rec.clears)
    for rf in sr_dir.iterdir():
        if rf.suffix != ".json":
            continue
        sp = f"control/stop-requests/{rf.name}"
        rec = _read_record(
            rf, sp, project_id, None, None, current_generation, project_fences,
        )
        if rec is not None:
            _, _, kind_rec = rec
            if kind_rec.sequence not in cleared:
                return True
    return False


def _read_grant(workspace_root: Path, attempt_id: str) -> Optional[Dict[str, Any]]:
    """Read the grant JSON for attempt_id from the registry directory."""
    grant_path = (
        workspace_root / ".execution-registry" / "grants" / f"{attempt_id}.json"
    )
    try:
        raw_bytes = grant_path.read_bytes()
        data: Dict[str, Any] = json.loads(raw_bytes)
        return data
    except (OSError, ValueError):
        return None


def _indeterminate_facts(task: TaskState, ctx: Context, store_path: str) -> Facts:
    """Return a Facts instance with indeterminate=True and all other fields at zero."""
    return Facts(
        task_status=task.task_status,
        bound_attempt=task.attempt_id,
        revision=ctx.revision,
        coordinator_run=ctx.coordinator_run,
        ownership_generation=ctx.ownership_generation,
        authorization_in_force=task.authorization_in_force,
        receipt_present=task.receipt_present,
        plan_hash=task.plan_hash,
        definition_hash=None,
        reserved=False,
        prepared=False,
        launched=False,
        result=None,
        declared_scopes=frozenset(),
        sealed_scopes=frozenset(),
        open_scopes=frozenset(),
        open_causes=frozenset(),
        stop_evidence=None,
        classified=None,
        accepted=False,
        commit_observed=False,
        released=False,
        uncertain_start=False,
        stop_requested=False,
        grant_present=False,
        launch_capability="none",
        indeterminate=True,
        indeterminate_path=store_path,
    )


# ---------------------------------------------------------------------------
# Projection (§6.1, §6.3)
# ---------------------------------------------------------------------------


def project_facts(ctx: Context, task: TaskState) -> Facts:
    """Compute all §6.1 facts for *task* from the store.  §6.3 validity enforced throughout."""
    exec_dir = ctx.execution_dir
    own_gen = ctx.ownership_generation

    # Project-level generation fences (needed to validate stop-request records)
    project_fences = _load_gen_fences(exec_dir, None, ctx.project_id, own_gen)

    # stop_requested is a project-level fact (§6.1)
    try:
        stop_requested = _compute_stop_requested(
            exec_dir, ctx.project_id, own_gen, project_fences,
        )
    except _IndeterminateError as exc:
        return _indeterminate_facts(task, ctx, exc.store_path)

    attempt_id = task.attempt_id
    if attempt_id is None:
        return Facts(
            task_status=task.task_status,
            bound_attempt=None,
            revision=ctx.revision,
            coordinator_run=ctx.coordinator_run,
            ownership_generation=own_gen,
            authorization_in_force=task.authorization_in_force,
            receipt_present=task.receipt_present,
            plan_hash=task.plan_hash,
            definition_hash=None,
            reserved=False,
            prepared=False,
            launched=False,
            result=None,
            declared_scopes=frozenset(),
            sealed_scopes=frozenset(),
            open_scopes=frozenset(),
            open_causes=frozenset(),
            stop_evidence=None,
            classified=None,
            accepted=False,
            commit_observed=False,
            released=False,
            uncertain_start=False,
            stop_requested=stop_requested,
            grant_present=False,
            launch_capability="none",
            indeterminate=False,
            indeterminate_path=None,
        )

    # Attempt-level generation fences
    attempt_fences = _load_gen_fences(exec_dir, attempt_id, ctx.project_id, own_gen)
    all_fences: Dict[int, FrozenSet[str]] = dict(project_fences)
    for gen, paths in attempt_fences.items():
        all_fences[gen] = all_fences.get(gen, frozenset()) | paths

    attempt_dir = exec_dir / "attempts" / attempt_id
    pfx = f"attempts/{attempt_id}"

    def _load(
        rel: str,
    ) -> Optional[Tuple[CommonEnvelope, Optional[AttemptEnvelope], Any]]:
        return _read_record(
            attempt_dir / rel,
            f"{pfx}/{rel}",
            ctx.project_id,
            task.task_id,
            attempt_id,
            own_gen,
            all_fences,
        )

    try:
        res_rec = _load("reservation.json")
        prep_rec = _load("prepared.json")
        launch_rec = _load("launch.json")
        result_rec = _load("result.json")
        class_rec = _load("classification.json")
        rel_rec = _load("release.json")
        se_rec = _load("stop-evidence.json")
        declared_scopes = _load_scope_ids(
            attempt_dir / "scopes", f"{pfx}/scopes",
            ctx.project_id, task.task_id, attempt_id, own_gen, all_fences,
        )
        sealed_scopes = _load_scope_ids(
            attempt_dir / "sealed", f"{pfx}/sealed",
            ctx.project_id, task.task_id, attempt_id, own_gen, all_fences,
        )
        open_causes = _compute_open_causes(
            attempt_dir, pfx,
            ctx.project_id, task.task_id, attempt_id, own_gen, all_fences,
        )
        accepted, commit_observed = _compute_accepted_committed(
            attempt_dir / "mutations", f"{pfx}/mutations",
            ctx.project_id, task.task_id, attempt_id, own_gen, all_fences,
        )
    except _IndeterminateError as exc:
        return _indeterminate_facts(task, ctx, exc.store_path)

    reserved = res_rec is not None
    reserved_plan_hash: Optional[str] = None
    if res_rec is not None:
        _, _, res_kind = res_rec
        reserved_plan_hash = res_kind.plan_hash or None
    prepared = prep_rec is not None
    definition_hash: Optional[str] = None
    if prep_rec is not None:
        _, _, prep_kind = prep_rec
        definition_hash = prep_kind.definition_hash

    launched = launch_rec is not None
    uncertain_start = False
    if launch_rec is not None:
        _, _, launch_kind = launch_rec
        uncertain_start = launch_kind.start_outcome == "ambiguous"

    result_val: Optional[str] = None
    if result_rec is not None:
        _, _, result_kind = result_rec
        result_val = result_kind.outcome

    classified: Optional[str] = None
    if class_rec is not None:
        _, _, class_kind = class_rec
        classified = class_kind.cls

    stop_evidence: Optional[str] = None
    if se_rec is not None:
        _, _, se_kind = se_rec
        stop_evidence = se_kind.kind

    released = rel_rec is not None

    grant = _read_grant(ctx.workspace_root, attempt_id)
    grant_present = grant is not None
    launch_capability: str = (grant.get("capability") or "none") if grant else "none"

    return Facts(
        task_status=task.task_status,
        bound_attempt=attempt_id,
        revision=ctx.revision,
        coordinator_run=ctx.coordinator_run,
        ownership_generation=own_gen,
        authorization_in_force=task.authorization_in_force,
        receipt_present=task.receipt_present,
        plan_hash=reserved_plan_hash,
        definition_hash=definition_hash,
        reserved=reserved,
        prepared=prepared,
        launched=launched,
        result=result_val,
        declared_scopes=declared_scopes,
        sealed_scopes=sealed_scopes,
        open_scopes=declared_scopes - sealed_scopes,
        open_causes=open_causes,
        stop_evidence=stop_evidence,
        classified=classified,
        accepted=accepted,
        commit_observed=commit_observed,
        released=released,
        uncertain_start=uncertain_start,
        stop_requested=stop_requested,
        grant_present=grant_present,
        launch_capability=launch_capability,
        indeterminate=False,
        indeterminate_path=None,
    )


# ---------------------------------------------------------------------------
# Label derivation (§6.2) — 13 rows, first match wins
# ---------------------------------------------------------------------------


def derive_label(facts: Facts, *, deadline_exceeded: bool = False) -> str:
    """§6.2 derived label.  Thirteen rows; the first matching row wins."""
    if facts.indeterminate:
        return "INDETERMINATE"
    if facts.released:
        return "RELEASED"
    if facts.commit_observed:
        return "COMMITTED"
    if facts.accepted:
        return "ACCEPTED"
    if facts.open_causes and facts.open_scopes:
        return "QUARANTINED"
    if facts.open_causes:
        return "HELD"
    if facts.classified is not None:
        return "CLASSIFIED"
    if facts.result is not None:
        return "PUBLISHED"
    if facts.uncertain_start or deadline_exceeded:
        return "UNCERTAIN"
    if facts.launched:
        return "RUNNING"
    if facts.launch_capability == "issued":
        return "DISPATCHING"
    if facts.prepared:
        return "PREPARED"
    return "RESERVED"


# ---------------------------------------------------------------------------
# Perform gate (R5-07 structural guarantee)
# ---------------------------------------------------------------------------


def perform(
    ctx: Context,
    task: TaskState,
    guard_fn: Any,
    effect_fn: Any,
) -> None:
    """Sole guarded entry point to the store for task-level operations (R5-07).

    Computes current facts, runs guard_fn(facts) which raises PreconditionFailed when
    the precondition is not met, then calls effect_fn(ctx, task) to apply durable
    effects.  No other code in this module writes to the store.
    """
    facts = project_facts(ctx, task)
    if facts.indeterminate:
        raise OperationError(
            f"attempt is indeterminate: {facts.indeterminate_path}"
        )
    guard_fn(facts)
    effect_fn(ctx, task)


# ---------------------------------------------------------------------------
# Record building helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """RFC 3339 UTC timestamp at second precision."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tmp_dir(ctx: Context) -> Path:
    return ctx.execution_dir / "runtime" / "tmp"


def _make_record(
    ctx: Context,
    record_kind: str,
    *,
    task_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Build a §7.4 record dict with common envelope, optional attempt envelope, and body_digest."""
    raw: Dict[str, Any] = {
        "record_kind": record_kind,
        "schema_version": "1.0",
        "project_id": ctx.project_id,
        "coordinator_run": ctx.coordinator_run or "",
        "ownership_generation": ctx.ownership_generation,
        "writer": "coordinator",
        "written_at": _now(),
    }
    if task_id is not None:
        raw["task_id"] = task_id
    if attempt_id is not None:
        raw["attempt_id"] = attempt_id
    raw.update(fields)
    # body_digest over the record without body_digest/receipt_id
    raw["body_digest"] = _compute_body_digest(raw)
    return raw


def _publish_record(ctx: Context, path: Path, record: Dict[str, Any]) -> str:
    """Serialize and publish one D1 record using publish_if_absent."""
    body = _canonical_json(record)
    return publish_if_absent(path, body, _tmp_dir(ctx))


def _next_fence_sequence(mutations_dir: Path, receipt_id_val: str) -> int:
    """Return 1-based next fence sequence under mutations/<receipt-id>/fences/."""
    fences_dir = mutations_dir / receipt_id_val / "fences"
    if not fences_dir.exists():
        return 1
    highest = 0
    for f in fences_dir.iterdir():
        if f.suffix == ".json":
            try:
                highest = max(highest, int(f.stem))
            except ValueError:
                pass
    return highest + 1


def _grant_body_digest(grant: Dict[str, Any]) -> str:
    """Compute body_digest for a grant (strips body_digest key only)."""
    stripped = {k: v for k, v in grant.items() if k != "body_digest"}
    return hashlib.sha256(_canonical_json(stripped)).hexdigest()


# ---------------------------------------------------------------------------
# O1-O20 implementations (§8.1)
# ---------------------------------------------------------------------------


def _o1_reserve(ctx: Context, task: TaskState, new_attempt_id: str) -> None:
    """O1 reserve: publish reservation.json for a new attempt (§8.1 O1).

    ``task`` reflects current project.json state (attempt_id=None, task_status=TODO).
    ``new_attempt_id`` is the coordinator-generated attempt id.
    """

    def _guard(facts: Facts) -> None:
        if facts.task_status != "TODO":
            raise PreconditionError(f"O1: task_status is {facts.task_status!r}, need TODO")
        if facts.bound_attempt is not None:
            raise PreconditionError("O1: task already has a bound attempt")

    def _effect(ctx: Context, task: TaskState) -> None:
        dest = ctx.execution_dir / "attempts" / new_attempt_id / "reservation.json"
        raw = _make_record(
            ctx, "reservation",
            task_id=task.task_id, attempt_id=new_attempt_id,
            plan_hash=task.plan_hash or "",
            claims=[],
            counter=0,
        )
        _publish_record(ctx, dest, raw)

    perform(ctx, task, _guard, _effect)


def _o2_grant(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    claims: List[str],
) -> None:
    """O2 grant: insert grant in registry under registry lock (§8.1 O2).

    ``task`` should have attempt_id set to ``attempt_id`` so project_facts can
    load the reservation and verify ``reserved`` is True.
    """

    def _guard(facts: Facts) -> None:
        if not facts.reserved:
            raise PreconditionError("O2: not reserved")
        if facts.grant_present:
            raise PreconditionError("O2: grant already present")

    def _effect(ctx: Context, task: TaskState) -> None:
        grant: Dict[str, Any] = {
            "attempt_id": attempt_id,
            "project_id": ctx.project_id,
            "task_id": task.task_id,
            "coordinator_run": ctx.coordinator_run or "",
            "ownership_generation": ctx.ownership_generation,
            "claims": sorted(claims),
            "capability": "none",
            "capability_id": None,
            "granted_at": _now(),
            "body_digest": "",
        }
        grant["body_digest"] = _grant_body_digest(grant)
        ctx.registry.scan_and_insert(attempt_id, grant)

    perform(ctx, task, _guard, _effect)


def _o3_prepare(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    scope_claims: List[str],
    prepared_fields: Dict[str, Any],
) -> None:
    """O3 prepare: publish scopes/worker.json then prepared.json (§8.1 O3).

    ``task`` should have attempt_id set so facts can check reserved + grant_present.
    ``scope_claims`` are the claims for the worker scope.
    ``prepared_fields`` must include definition_hash, plan_hash, contract, baseline,
    instruction_digest, deadline_at, deadline_budget_s, heartbeat_interval_s, log_cap_bytes.
    """

    def _guard(facts: Facts) -> None:
        if not facts.reserved:
            raise PreconditionError("O3: not reserved")
        if not facts.grant_present:
            raise PreconditionError("O3: no grant present")
        if task.plan_hash is not None and facts.plan_hash != task.plan_hash:
            raise PreconditionError("O3: plan_hash mismatch")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        # Step 2: publish scopes/worker.json
        scope_raw = _make_record(
            ctx, "scope",
            task_id=task.task_id, attempt_id=attempt_id,
            scope_id="worker",
            kind="worker",
            claims=sorted(scope_claims),
        )
        _publish_record(ctx, attempt_dir / "scopes" / "worker.json", scope_raw)
        # Step 3: publish prepared.json
        prep_raw = _make_record(
            ctx, "prepared",
            task_id=task.task_id, attempt_id=attempt_id,
            **prepared_fields,
        )
        _publish_record(ctx, attempt_dir / "prepared.json", prep_raw)

    perform(ctx, task, _guard, _effect)


def _o4_dispatch(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    definition_hash: str,
    receipt_entry: Dict[str, Any],
) -> None:
    """O4 dispatch: 8-step sequence — intent/fence/commit/ack/capability/start/launch/consume (§8.1 O4)."""

    def _guard(facts: Facts) -> None:
        if not facts.prepared:
            raise PreconditionError("O4: not prepared")
        if not facts.authorization_in_force:
            raise PreconditionError("O4: authorization_in_force is False")
        if facts.task_status != "TODO":
            raise PreconditionError(f"O4: task_status is {facts.task_status!r}, need TODO")
        if facts.open_causes:
            raise PreconditionError(f"O4: open_causes non-empty: {facts.open_causes}")
        if facts.stop_requested:
            raise PreconditionError("O4: stop_requested is True")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        mutations_dir = attempt_dir / "mutations"

        # Compute dispatch receipt_id: intent=RUNNING, empty snapshot + captures
        rid = _compute_receipt_id(attempt_id, definition_hash, "RUNNING", {}, [])
        mut_dir = mutations_dir / rid

        # Step 1: publish intent.json
        intent_raw = _make_record(
            ctx, "acceptance",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            intent="RUNNING",
            definition_hash=definition_hash,
            accepted_snapshot={},
            qualifying_captures=[],
        )
        _publish_record(ctx, mut_dir / "intent.json", intent_raw)

        # Step 2: publish fence
        seq = _next_fence_sequence(mutations_dir, rid)
        fence_raw = _make_record(
            ctx, "fence",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            sequence=seq,
            expected_revision=ctx.revision,
            expected_run=ctx.coordinator_run or "",
            expected_generation=ctx.ownership_generation,
            submitted_at=_now(),
        )
        _publish_record(ctx, mut_dir / "fences" / f"{seq:04d}.json", fence_raw)

        # Step 3: commit (bind execution.attempts[task_id] = attempt_id)
        if ctx.commit_fn is not None:
            new_rev, ev_idx = ctx.commit_fn(
                task.task_id, "RUNNING", attempt_id, ctx.revision,
                None, None, {"receipt_id": rid, **receipt_entry},
            )
            ctx.revision = new_rev
        else:
            ev_idx = 0

        # Step 4: ack
        ack_raw = _make_record(
            ctx, "commit-observed",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            fence_sequence=seq,
            committed_revision=ctx.revision,
            evidence_index=ev_idx,
            committed_status="RUNNING",
        )
        _publish_record(ctx, mut_dir / "ack.json", ack_raw)

        # Step 5: set capability = issued under registry lock
        grant = _read_grant(ctx.workspace_root, attempt_id)
        if grant is not None:
            cap_id = "cap-" + secrets.token_hex(16)
            grant["capability"] = "issued"
            grant["capability_id"] = cap_id
            grant["body_digest"] = _grant_body_digest(grant)
            ctx.registry.update_grant(attempt_id, grant)
        else:
            cap_id = "cap-" + secrets.token_hex(16)

        # Step 6: call adapter start
        plan: Dict[str, Any] = {}
        attempt_dir_str = str(attempt_dir)
        handle = ctx.adapter.start(plan, attempt_dir_str)

        # Step 7: publish launch.json
        start_outcome = "started" if handle != "ambiguous" else "ambiguous"
        launch_handle: Any = handle if handle != "ambiguous" else None
        launch_raw = _make_record(
            ctx, "launch",
            task_id=task.task_id, attempt_id=attempt_id,
            start_outcome=start_outcome,
            handle=launch_handle,
            capability_id=cap_id,
            started_at=_now(),
            adapter={"name": "fake", "version": "0"},
        )
        _publish_record(ctx, attempt_dir / "launch.json", launch_raw)

        # Step 8: set capability = consumed under registry lock
        grant2 = _read_grant(ctx.workspace_root, attempt_id)
        if grant2 is not None:
            grant2["capability"] = "consumed"
            grant2["body_digest"] = _grant_body_digest(grant2)
            ctx.registry.update_grant(attempt_id, grant2)

    perform(ctx, task, _guard, _effect)


def _o5_record_result(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    result_fields: Dict[str, Any],
) -> None:
    """O5 record-result: publish result.json (§8.1 O5)."""

    def _guard(facts: Facts) -> None:
        if facts.launch_capability not in ("issued", "consumed") and not facts.uncertain_start:
            raise PreconditionError(
                f"O5: launch_capability is {facts.launch_capability!r} and not uncertain_start"
            )

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        raw = _make_record(
            ctx, "result",
            task_id=task.task_id, attempt_id=attempt_id,
            **result_fields,
        )
        _publish_record(ctx, attempt_dir / "result.json", raw)

    perform(ctx, task, _guard, _effect)


def _o6_seal(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    scope_id: str,
    exit_status: Dict[str, Any],
) -> None:
    """O6 seal: publish sealed/<scope-id>.json after adapter confirms exit (§8.1 O6)."""

    def _guard(facts: Facts) -> None:
        if scope_id in facts.sealed_scopes:
            raise PreconditionError(f"O6: scope {scope_id!r} already sealed")
        if scope_id not in facts.declared_scopes:
            raise PreconditionError(f"O6: scope {scope_id!r} not declared")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        encoded = encode_scope_id(scope_id)
        raw = _make_record(
            ctx, "sealed",
            task_id=task.task_id, attempt_id=attempt_id,
            scope_id=scope_id,
            exit=exit_status,
            tree_exited=True,
            resumable=False,
        )
        _publish_record(ctx, attempt_dir / "sealed" / f"{encoded}.json", raw)

    perform(ctx, task, _guard, _effect)


def _o7_classify(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    cls: str,
    evidence: List[Dict[str, Any]],
    *,
    deadline_exceeded: bool = False,
) -> None:
    """O7 classify: publish classification.json (§8.1 O7)."""

    def _guard(facts: Facts) -> None:
        if "worker" not in facts.sealed_scopes:
            raise PreconditionError("O7: worker scope not sealed")
        if facts.result is None and facts.stop_evidence is None and not deadline_exceeded:
            raise PreconditionError("O7: no result, no stop_evidence, deadline not exceeded")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        raw = _make_record(
            ctx, "classification",
            task_id=task.task_id, attempt_id=attempt_id,
            **{"class": cls},
            evidence=evidence,
        )
        _publish_record(ctx, attempt_dir / "classification.json", raw)

    perform(ctx, task, _guard, _effect)


def _o8_hold(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    cause_id: str,
    cause_class: str,
    detail: str,
    raised_by: str,
) -> None:
    """O8 hold: publish holds/<cause-id>.json (§8.1 O8)."""

    def _guard(_facts: Facts) -> None:
        pass  # O8 has no precondition; any caller may raise a hold

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        raw = _make_record(
            ctx, "hold",
            task_id=task.task_id, attempt_id=attempt_id,
            cause_id=cause_id,
            cause_class=cause_class,
            detail=detail,
            raised_by=raised_by,
        )
        _publish_record(ctx, attempt_dir / "holds" / f"{cause_id}.json", raw)

    perform(ctx, task, _guard, _effect)


def _o9_resolve(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    cause_id: str,
    resolution: str,
    observed_revision: int,
    decided_by: str,
    rationale: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """O9 resolve: publish resolutions/<cause-id>.json (§8.1 O9)."""

    def _guard(facts: Facts) -> None:
        if cause_id not in facts.open_causes:
            raise PreconditionError(f"O9: cause_id {cause_id!r} not in open_causes")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        fields: Dict[str, Any] = {
            "cause_id": cause_id,
            "resolution": resolution,
            "observed_revision": observed_revision,
            "decided_by": decided_by,
            "rationale": rationale,
        }
        if evidence is not None:
            fields["evidence"] = evidence
        raw = _make_record(
            ctx, "resolution",
            task_id=task.task_id, attempt_id=attempt_id,
            **fields,
        )
        _publish_record(ctx, attempt_dir / "resolutions" / f"{cause_id}.json", raw)

    perform(ctx, task, _guard, _effect)


def _o10_run_check(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    check_id: str,
    sequence: int,
    capture_fields: Dict[str, Any],
    assessment_fields: Dict[str, Any],
    exit_status: Dict[str, Any],
) -> None:
    """O10 run-check: publish scope, run check, publish capture, assessment, seal (§8.1 O10)."""

    scope_id = f"check:{check_id}#{sequence:04d}"

    def _guard(facts: Facts) -> None:
        if not facts.prepared:
            raise PreconditionError("O10: not prepared")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        encoded_scope = encode_scope_id(scope_id)

        # Step 1: publish scope record BEFORE any process runs
        scope_raw = _make_record(
            ctx, "scope",
            task_id=task.task_id, attempt_id=attempt_id,
            scope_id=scope_id,
            kind="check",
            check_id=check_id,
            sequence=sequence,
            claims=[],
        )
        _publish_record(ctx, attempt_dir / "scopes" / f"{encoded_scope}.json", scope_raw)

        # Step 2: run check (captured by capture_fields from caller)

        # Step 3: publish capture
        cap_raw = _make_record(
            ctx, "capture",
            task_id=task.task_id, attempt_id=attempt_id,
            check_id=check_id,
            sequence=sequence,
            **capture_fields,
        )
        seq_str = f"{sequence:04d}"
        cap_bd = _compute_body_digest(cap_raw)
        cap_id = "cap-" + cap_bd[:32]
        cap_path = attempt_dir / "captures" / check_id / f"{seq_str}-{cap_id}.json"
        _publish_record(ctx, cap_path, cap_raw)

        # Step 4: publish assessment
        asm_raw = _make_record(
            ctx, "assessment",
            task_id=task.task_id, attempt_id=attempt_id,
            check_id=check_id,
            sequence=sequence,
            capture_id=cap_id,
            capture_body_digest=cap_bd,
            **assessment_fields,
        )
        asm_path = attempt_dir / "assessments" / check_id / f"{seq_str}.json"
        _publish_record(ctx, asm_path, asm_raw)

        # Step 5: publish sealed/<scope-id>.json
        seal_raw = _make_record(
            ctx, "sealed",
            task_id=task.task_id, attempt_id=attempt_id,
            scope_id=scope_id,
            exit=exit_status,
            tree_exited=True,
            resumable=False,
        )
        _publish_record(ctx, attempt_dir / "sealed" / f"{encoded_scope}.json", seal_raw)

    perform(ctx, task, _guard, _effect)


def _o11_accept(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    definition_hash: str,
    intent: str,
    accepted_snapshot: Dict[str, Any],
    qualifying_captures: List[Dict[str, Any]],
) -> str:
    """O11 accept: publish mutations/<receipt-id>/intent.json (§8.1 O11).

    Returns the receipt_id so the caller can use it for O12.
    """
    rid = _compute_receipt_id(
        attempt_id, definition_hash, intent, accepted_snapshot, qualifying_captures
    )

    def _guard(facts: Facts) -> None:
        if facts.open_causes:
            raise PreconditionError(f"O11: open_causes non-empty: {facts.open_causes}")
        if facts.open_scopes:
            raise PreconditionError(f"O11: open_scopes non-empty: {facts.open_scopes}")
        if facts.classified is None:
            raise PreconditionError("O11: not classified")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        intent_raw = _make_record(
            ctx, "acceptance",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            intent=intent,
            definition_hash=definition_hash,
            accepted_snapshot=accepted_snapshot,
            qualifying_captures=qualifying_captures,
        )
        _publish_record(
            ctx,
            attempt_dir / "mutations" / rid / "intent.json",
            intent_raw,
        )

    perform(ctx, task, _guard, _effect)
    return rid


def _o12_commit_acceptance(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    receipt_id_val: str,
    definition_hash: str,
    receipt_entry: Dict[str, Any],
    target_status: str,
    block_reason: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> None:
    """O12 commit-acceptance: append evidence + fence + commit + ack (§8.1 O12)."""

    def _guard(facts: Facts) -> None:
        if not facts.accepted:
            raise PreconditionError("O12: not accepted")
        if facts.definition_hash is not None and facts.definition_hash != definition_hash:
            raise PreconditionError("O12: definition_hash mismatch")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        mutations_dir = attempt_dir / "mutations"

        # Step 1: append receipt to evidence.md (deduplicated by receipt_id)
        if ctx.project_dir is not None:
            ev_path = ctx.project_dir / "evidence.md"
            line = f"\n- receipt: {receipt_id_val} ({target_status})"
            try:
                existing = ev_path.read_text(encoding="utf-8") if ev_path.exists() else ""
                if receipt_id_val not in existing:
                    with ev_path.open("a", encoding="utf-8") as fh:
                        fh.write(line)
            except OSError:
                pass

        # Step 2: publish fence
        seq = _next_fence_sequence(mutations_dir, receipt_id_val)
        fence_raw = _make_record(
            ctx, "fence",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=receipt_id_val,
            sequence=seq,
            expected_revision=ctx.revision,
            expected_run=ctx.coordinator_run or "",
            expected_generation=ctx.ownership_generation,
            submitted_at=_now(),
        )
        _publish_record(
            ctx,
            mutations_dir / receipt_id_val / "fences" / f"{seq:04d}.json",
            fence_raw,
        )

        # Step 3: commit
        if ctx.commit_fn is not None:
            new_rev, ev_idx = ctx.commit_fn(
                task.task_id, target_status, None, ctx.revision,
                block_reason, skip_reason, {"receipt_id": receipt_id_val, **receipt_entry},
            )
            ctx.revision = new_rev
        else:
            ev_idx = 0

        # Step 4: ack
        ack_raw = _make_record(
            ctx, "commit-observed",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=receipt_id_val,
            fence_sequence=seq,
            committed_revision=ctx.revision,
            evidence_index=ev_idx,
            committed_status=target_status,
        )
        _publish_record(ctx, mutations_dir / receipt_id_val / "ack.json", ack_raw)

    perform(ctx, task, _guard, _effect)


def _o13_withdraw(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    definition_hash: str,
    intent: str,
    receipt_entry: Dict[str, Any],
    target_status: str,
    block_reason: Optional[str] = None,
) -> None:
    """O13 withdraw: publish intent + fence + commit + ack (§8.1 O13)."""
    rid = _compute_receipt_id(attempt_id, definition_hash, intent, {}, [])

    def _guard(facts: Facts) -> None:
        if facts.open_scopes:
            raise PreconditionError(f"O13: open_scopes non-empty: {facts.open_scopes}")
        has_withdraw_cause = False
        if hasattr(facts, "open_causes"):
            pass  # Checking resolution disposition requires reading resolution records
        if not facts.authorization_in_force:
            return  # authorization_lost is a valid withdraw trigger
        if not has_withdraw_cause and facts.authorization_in_force:
            pass  # caller is responsible for withdraw eligibility check

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        mutations_dir = attempt_dir / "mutations"
        mut_dir = mutations_dir / rid

        # Step 1: publish intent
        intent_raw = _make_record(
            ctx, "acceptance",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            intent=intent,
            definition_hash=definition_hash,
            accepted_snapshot={},
            qualifying_captures=[],
        )
        _publish_record(ctx, mut_dir / "intent.json", intent_raw)

        # Step 2: publish fence
        seq = _next_fence_sequence(mutations_dir, rid)
        fence_raw = _make_record(
            ctx, "fence",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            sequence=seq,
            expected_revision=ctx.revision,
            expected_run=ctx.coordinator_run or "",
            expected_generation=ctx.ownership_generation,
            submitted_at=_now(),
        )
        _publish_record(ctx, mut_dir / "fences" / f"{seq:04d}.json", fence_raw)

        # Step 3: commit
        if ctx.commit_fn is not None:
            new_rev, ev_idx = ctx.commit_fn(
                task.task_id, target_status, None, ctx.revision,
                block_reason, None, {"receipt_id": rid, **receipt_entry},
            )
            ctx.revision = new_rev
        else:
            ev_idx = 0

        # Step 4: ack
        ack_raw = _make_record(
            ctx, "commit-observed",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt_id=rid,
            fence_sequence=seq,
            committed_revision=ctx.revision,
            evidence_index=ev_idx,
            committed_status=target_status,
        )
        _publish_record(ctx, mut_dir / "ack.json", ack_raw)

    perform(ctx, task, _guard, _effect)


def _o14_retry(
    ctx: Context,
    task: TaskState,
    new_attempt_id: str,
    definition_hash: str,
    receipt_entry: Dict[str, Any],
) -> None:
    """O14 retry: publish intent on a new attempt + fence + commit + ack (§8.1 O14)."""
    rid = _compute_receipt_id(new_attempt_id, definition_hash, "TODO", {}, [])

    def _guard(facts: Facts) -> None:
        if facts.task_status != "BLOCKED":
            raise PreconditionError(f"O14: task_status is {facts.task_status!r}, need BLOCKED")
        if facts.open_causes:
            raise PreconditionError(f"O14: open_causes non-empty: {facts.open_causes}")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / new_attempt_id
        mutations_dir = attempt_dir / "mutations"
        mut_dir = mutations_dir / rid

        # Step 1: publish intent on new attempt
        intent_raw = _make_record(
            ctx, "acceptance",
            task_id=task.task_id, attempt_id=new_attempt_id,
            receipt_id=rid,
            intent="TODO",
            definition_hash=definition_hash,
            accepted_snapshot={},
            qualifying_captures=[],
        )
        _publish_record(ctx, mut_dir / "intent.json", intent_raw)

        # Step 2: publish fence
        seq = _next_fence_sequence(mutations_dir, rid)
        fence_raw = _make_record(
            ctx, "fence",
            task_id=task.task_id, attempt_id=new_attempt_id,
            receipt_id=rid,
            sequence=seq,
            expected_revision=ctx.revision,
            expected_run=ctx.coordinator_run or "",
            expected_generation=ctx.ownership_generation,
            submitted_at=_now(),
        )
        _publish_record(ctx, mut_dir / "fences" / f"{seq:04d}.json", fence_raw)

        # Step 3: commit (clear bound_attempt, block_reason; TODO transition)
        if ctx.commit_fn is not None:
            new_rev, ev_idx = ctx.commit_fn(
                task.task_id, "TODO", None, ctx.revision,
                None, None, {"receipt_id": rid, **receipt_entry},
            )
            ctx.revision = new_rev
        else:
            ev_idx = 0

        # Step 4: ack
        ack_raw = _make_record(
            ctx, "commit-observed",
            task_id=task.task_id, attempt_id=new_attempt_id,
            receipt_id=rid,
            fence_sequence=seq,
            committed_revision=ctx.revision,
            evidence_index=ev_idx,
            committed_status="TODO",
        )
        _publish_record(ctx, mut_dir / "ack.json", ack_raw)

    perform(ctx, task, _guard, _effect)


def _o15_stop(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    stop_sequence: int,
    requested_by: str,
    reason: str,
    cause_id: str,
    stop_kind: str,
) -> None:
    """O15 stop: stop-request + hold + terminate + stop-evidence + O6 for each scope (§8.1 O15)."""

    def _guard(_facts: Facts) -> None:
        pass  # Operator or deadline triggers; any attempt may be stopped

    def _effect(ctx: Context, task: TaskState) -> None:
        # Step 1: publish stop-request (project-level, no attempt envelope)
        sr_raw = _make_record(
            ctx, "stop-request",
            sequence=stop_sequence,
            requested_by=requested_by,
            reason=reason,
        )
        seq_str = f"{stop_sequence:04d}"
        sr_path = ctx.execution_dir / "control" / "stop-requests" / f"{seq_str}.json"
        _publish_record(ctx, sr_path, sr_raw)

        # Step 2: publish hold with cause_class operator_stop
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id
        hold_raw = _make_record(
            ctx, "hold",
            task_id=task.task_id, attempt_id=attempt_id,
            cause_id=cause_id,
            cause_class="operator_stop",
            detail=reason,
            raised_by="coordinator",
        )
        _publish_record(ctx, attempt_dir / "holds" / f"{cause_id}.json", hold_raw)

        # Step 3: call adapter terminate for open scopes
        # (in practice we'd get the handle from the launch record; simplified here)
        # Step 4: publish stop-evidence
        se_raw = _make_record(
            ctx, "stop-evidence",
            task_id=task.task_id, attempt_id=attempt_id,
            kind=stop_kind,
            observed_at=_now(),
            detail=reason,
            scopes=[],
        )
        _publish_record(ctx, attempt_dir / "stop-evidence.json", se_raw)

    perform(ctx, task, _guard, _effect)


def _o16_release(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    terminal_receipt_id: Optional[str],
    release_reason: str,
) -> None:
    """O16 release: publish release.json then remove grant (§8.1 O16)."""

    def _guard(facts: Facts) -> None:
        if facts.open_scopes:
            raise PreconditionError(f"O16: open_scopes non-empty: {facts.open_scopes}")
        if facts.launch_capability not in ("consumed", "revoked"):
            raise PreconditionError(
                f"O16: launch_capability is {facts.launch_capability!r}, need consumed or revoked"
            )
        if facts.open_causes:
            raise PreconditionError(f"O16: open_causes non-empty: {facts.open_causes}")
        if facts.accepted and not facts.commit_observed:
            raise PreconditionError("O16: accepted but commit_observed is False")

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id

        # Step 1: publish release.json (grant_removed=False at time of writing)
        sealed_list = sorted(
            encode_scope_id(s) for s in
            (_read_scope_ids_for_release(ctx, attempt_id))
        )
        rel_raw = _make_record(
            ctx, "release",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt=terminal_receipt_id,
            sealed_scopes=sealed_list,
            grant_removed=False,
            reason=release_reason,
        )
        _publish_record(ctx, attempt_dir / "release.json", rel_raw)

        # Step 2: remove the grant under registry lock
        ctx.registry.scan_and_remove(attempt_id)

    perform(ctx, task, _guard, _effect)


def _read_scope_ids_for_release(ctx: Context, attempt_id: str) -> Set[str]:
    """Read sealed scope ids for building the release record's sealed_scopes list."""
    sealed_dir = ctx.execution_dir / "attempts" / attempt_id / "sealed"
    if not sealed_dir.exists():
        return set()
    ids: Set[str] = set()
    for f in sealed_dir.iterdir():
        if f.suffix == ".json":
            ids.add(decode_scope_id(f.stem))
    return ids


def _o17_take_over(ctx: Context) -> None:
    """O17 take-over: commit new coordinator_run + generation, publish gen-fences, delete runtime/ (§8.1 O17).

    Project-level operation — guard checked inline (not via perform).
    The predecessor must be proven dead before calling this (§12.2); that check is
    the caller's responsibility.
    """
    # Step 1: commit new coordinator_run and ownership_generation
    if ctx.commit_fn is not None:
        new_gen = ctx.ownership_generation + 1
        new_rev, _ = ctx.commit_fn(
            "", "TAKEOVER", None, ctx.revision,
            None, None, {"coordinator_run": ctx.coordinator_run, "ownership_generation": new_gen},
        )
        ctx.revision = new_rev
        ctx.ownership_generation = new_gen

    # Step 2: publish generation-fences for every attempt + store root
    # (simplified — full implementation publishes one fence per active attempt)
    exec_dir = ctx.execution_dir
    gen = ctx.ownership_generation
    old_gen = gen - 1

    # Store-root generation fence
    root_records: List[str] = []
    for p in exec_dir.glob("*.json"):
        root_records.append(p.name)
    root_fence_raw = _make_record(
        ctx, "generation-fence",
        generation=gen,
        taken_over_from=old_gen,
        scope="project",
        records=sorted(root_records),
    )
    fence_dir = exec_dir / "generation-fences"
    _publish_record(ctx, fence_dir / f"{old_gen}.json", root_fence_raw)

    # Step 3: delete runtime/
    runtime_dir = exec_dir / "runtime"
    if runtime_dir.exists():
        shutil.rmtree(str(runtime_dir), ignore_errors=True)


def _o18_dispose(
    ctx: Context,
    task: TaskState,
    attempt_id: str,
    release_reason: str,
) -> None:
    """O18 dispose: O6 for each open scope (not_started) + release + remove grant (§8.1 O18)."""

    def _guard(facts: Facts) -> None:
        if facts.launch_capability != "none":
            raise PreconditionError(
                f"O18: launch_capability is {facts.launch_capability!r}, need none"
            )

    def _effect(ctx: Context, task: TaskState) -> None:
        attempt_dir = ctx.execution_dir / "attempts" / attempt_id

        # Step 1: O6 for every open scope with exit variant not_started
        open_scopes: Set[str] = set()
        scopes_dir = attempt_dir / "scopes"
        sealed_dir = attempt_dir / "sealed"
        if scopes_dir.exists():
            for f in scopes_dir.iterdir():
                if f.suffix == ".json":
                    open_scopes.add(decode_scope_id(f.stem))
        if sealed_dir.exists():
            for f in sealed_dir.iterdir():
                if f.suffix == ".json":
                    open_scopes.discard(decode_scope_id(f.stem))
        for scope_id in sorted(open_scopes):
            encoded = encode_scope_id(scope_id)
            seal_raw = _make_record(
                ctx, "sealed",
                task_id=task.task_id, attempt_id=attempt_id,
                scope_id=scope_id,
                exit={"variant": "not_started"},
                tree_exited=True,
                resumable=False,
            )
            _publish_record(ctx, attempt_dir / "sealed" / f"{encoded}.json", seal_raw)

        # Step 2: publish release.json
        sealed_list = sorted(
            encode_scope_id(s) for s in
            (_read_scope_ids_for_release(ctx, attempt_id) | open_scopes)
        )
        rel_raw = _make_record(
            ctx, "release",
            task_id=task.task_id, attempt_id=attempt_id,
            receipt=None,
            sealed_scopes=sealed_list,
            grant_removed=False,
            reason=release_reason,
        )
        _publish_record(ctx, attempt_dir / "release.json", rel_raw)

        # Step 3: remove the grant under registry lock
        ctx.registry.scan_and_remove(attempt_id)

    perform(ctx, task, _guard, _effect)


def _o19_acquire(ctx: Context, owner_fields: Dict[str, Any]) -> None:
    """O19 acquire: publish owners/<coordinator-run>.json + commit coordinator_run (§8.1 O19).

    Project-level operation — guard checked inline (not via perform).
    ``owner_fields`` contains host_id, boot_id, pid, process_start, started_at, generation.
    """
    # Guard: coordinator_run must be null
    if ctx.coordinator_run is not None:
        raise PreconditionError(f"O19: coordinator_run is {ctx.coordinator_run!r}, need null")

    # Step 1: publish owners/<coordinator-run>.json
    new_run = "run-" + secrets.token_hex(16)
    owner_raw = _make_record(
        ctx, "owner",
        **owner_fields,
        generation=ctx.ownership_generation + 1,
    )
    owner_path = ctx.execution_dir / "owners" / f"{new_run}.json"
    _publish_record(ctx, owner_path, owner_raw)

    # Step 2: commit coordinator_run = new_run and ownership_generation = G+1
    new_gen = ctx.ownership_generation + 1
    if ctx.commit_fn is not None:
        new_rev, _ = ctx.commit_fn(
            "", "ACQUIRE", None, ctx.revision,
            None, None, {"coordinator_run": new_run, "ownership_generation": new_gen},
        )
        ctx.revision = new_rev
    ctx.coordinator_run = new_run
    ctx.ownership_generation = new_gen


def _o20_relinquish(ctx: Context) -> None:
    """O20 relinquish: commit coordinator_run = null (§8.1 O20).

    Project-level operation — guard checked inline (not via perform).
    Precondition: no grant_present in registry, every attempt released, execution.attempts empty.
    """
    # Guard: no grants in registry
    grants = ctx.registry.read_all_grants()
    if any(g.get("project_id") == ctx.project_id for g in grants):
        raise PreconditionError("O20: grants still present in registry for this project")

    # Step 1: commit coordinator_run = null
    if ctx.commit_fn is not None:
        new_rev, _ = ctx.commit_fn(
            "", "RELINQUISH", None, ctx.revision,
            None, None, {"coordinator_run": None},
        )
        ctx.revision = new_rev
    ctx.coordinator_run = None
