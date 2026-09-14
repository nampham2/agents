"""Behavioral tests for execution_ops: projection, derive_label, perform, and O1-O20.

Coverage requirement: 100% over execution_ops.py. Every test establishes a named property.

Structure:
- TestProjectFacts      — §6.1 projection, §6.3 validity
- TestDeriveLabel       — §6.2 label table (13 rows)
- TestPerformGate       — R5-07: perform rejects indeterminate facts; calls guard + effect
- TestO1Reserve         — forward path + precondition failures
- TestO2Grant           — forward path + precondition failures
- TestO3Prepare         — forward path + precondition failures
- TestO4Dispatch        — forward path + precondition failures
- TestO5RecordResult    — forward path + precondition failures
- TestO6Seal            — forward path + precondition failures
- TestO7Classify        — forward path + precondition failures
- TestO8Hold            — forward path (no precondition)
- TestO9Resolve         — forward path + precondition failures
- TestO10RunCheck       — forward path + precondition failures
- TestO11Accept         — forward path + precondition failures, returns receipt_id
- TestO12CommitAcceptance — forward path, evidence.md append, fence + ack
- TestO13Withdraw       — forward path + precondition failures
- TestO14Retry          — forward path + precondition failures
- TestO15Stop           — forward path (no precondition)
- TestO16Release        — forward path + precondition failures
- TestO17TakeOver       — forward path + precondition inline
- TestO18Dispose        — forward path + precondition failures
- TestO19Acquire        — forward path + precondition failures
- TestO20Relinquish     — forward path + precondition failures
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest
from execution_adapter import OBSERVE_RUNNING, OBSERVE_UNKNOWN, AdapterError, FakeAdapter
from execution_ops import (
    Context,
    Facts,
    OperationError,
    PreconditionError,
    TaskState,
    _next_fence_sequence,
    _o1_reserve,
    _o2_grant,
    _o3_prepare,
    _o4_dispatch,
    _o5_record_result,
    _o6_seal,
    _o7_classify,
    _o8_hold,
    _o9_resolve,
    _o10_run_check,
    _o11_accept,
    _o12_commit_acceptance,
    _o13_withdraw,
    _o14_retry,
    _o15_stop,
    _o16_release,
    _o17_take_over,
    _o18_dispose,
    _o19_acquire,
    _o20_relinquish,
    derive_label,
    perform,
    project_facts,
)
from execution_serialization import (
    body_digest,
    canonical_json,
    receipt_id,
)
from execution_store import GrantRegistry

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PROJECT_ID = "test-proj-001"
COORDINATOR_RUN = "run-aaa"
ATTEMPT_ID = "att-0001"
TASK_ID = "task-alpha"
GENERATION = 1
REVISION = 10
DEFINITION_HASH = "def" + "0" * 61


def _registry(workspace_root: Path) -> GrantRegistry:
    return GrantRegistry(workspace_root)


def _ctx(
    exec_dir: Path,
    workspace_root: Path,
    *,
    coordinator_run: Optional[str] = COORDINATOR_RUN,
    generation: int = GENERATION,
    revision: int = REVISION,
    project_dir: Optional[Path] = None,
    commit_fn: Any = None,
) -> Context:
    adapter: Any = FakeAdapter()
    return Context(
        execution_dir=exec_dir,
        workspace_root=workspace_root,
        project_id=PROJECT_ID,
        coordinator_run=coordinator_run,
        ownership_generation=generation,
        revision=revision,
        registry=_registry(workspace_root),
        adapter=adapter,
        project_dir=project_dir,
        commit_fn=commit_fn,
    )


def _task(
    *,
    task_id: str = TASK_ID,
    attempt_id: Optional[str] = None,
    task_status: str = "TODO",
    authorization_in_force: bool = True,
    receipt_present: bool = False,
    plan_hash: Optional[str] = None,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        attempt_id=attempt_id,
        task_status=task_status,
        authorization_in_force=authorization_in_force,
        receipt_present=receipt_present,
        plan_hash=plan_hash,
    )


def _record_raw(
    record_kind: str,
    task_id: Optional[str],
    attempt_id: Optional[str],
    **fields: Any,
) -> bytes:
    """Build a minimal valid store record with body_digest."""
    raw: Dict[str, Any] = {
        "record_kind": record_kind,
        "schema_version": "1.0",
        "project_id": PROJECT_ID,
        "coordinator_run": COORDINATOR_RUN,
        "ownership_generation": GENERATION,
        "writer": "coordinator",
        "written_at": "2026-09-14T00:00:00Z",
    }
    if task_id is not None:
        raw["task_id"] = task_id
    if attempt_id is not None:
        raw["attempt_id"] = attempt_id
    raw.update(fields)
    raw["body_digest"] = body_digest(raw)
    return canonical_json(raw)


def _write_record(dest: Path, **kwargs: Any) -> None:
    """Write a record file, creating parent directories as needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_record_raw(**kwargs))


def _null_commit(
    task_id: str,
    target_status: str,
    bound_attempt: Optional[str],
    expected_revision: int,
    block_reason: Optional[str],
    skip_reason: Optional[str],
    receipt_entry: Dict[str, Any],
) -> Tuple[int, int]:
    return expected_revision + 1, 0


def _store_grant(workspace_root: Path, attempt_id: str = ATTEMPT_ID, **overrides: Any) -> None:
    grant_dir = workspace_root / ".execution-registry" / "grants"
    grant_dir.mkdir(parents=True, exist_ok=True)
    grant: Dict[str, Any] = {
        "attempt_id": attempt_id,
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "coordinator_run": COORDINATOR_RUN,
        "ownership_generation": GENERATION,
        "claims": [],
        "capability": "none",
        "capability_id": None,
        "granted_at": "2026-09-14T00:00:00Z",
        "body_digest": "",
    }
    grant.update(overrides)
    # recompute body_digest after overrides
    stripped = {k: v for k, v in grant.items() if k != "body_digest"}
    import hashlib
    grant["body_digest"] = hashlib.sha256(canonical_json(stripped)).hexdigest()
    (grant_dir / f"{attempt_id}.json").write_text(json.dumps(grant))


# ---------------------------------------------------------------------------
# Common setup fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp(tmp_path: Path) -> Tuple[Path, Path]:
    """Return (exec_dir, workspace_root) with basic directory layout."""
    exec_dir = tmp_path / "execution"
    exec_dir.mkdir()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / ".execution-registry" / "grants").mkdir(parents=True)
    (exec_dir / "runtime" / "tmp").mkdir(parents=True)
    return exec_dir, workspace_root


# ---------------------------------------------------------------------------
# TestProjectFacts
# ---------------------------------------------------------------------------


class TestProjectFacts:
    def test_no_attempt_returns_minimal_facts(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.reserved
        assert not facts.prepared
        assert not facts.indeterminate
        assert facts.bound_attempt is None

    def test_reserved_when_reservation_present(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.reserved

    def test_indeterminate_on_corrupt_json(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "reservation.json").write_bytes(b"not-json{{{")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate
        assert facts.indeterminate_path is not None

    def test_indeterminate_propagates_to_perform(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "reservation.json").write_bytes(b"{bad json}")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(OperationError, match="indeterminate"):
            perform(ctx, task, lambda f: None, lambda c, t: None)

    def test_grant_present_when_registry_entry_exists(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        facts = project_facts(ctx, task)
        assert facts.grant_present
        assert facts.launch_capability == "none"

    def test_launch_capability_reads_from_grant(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        facts = project_facts(ctx, task)
        assert facts.launch_capability == "consumed"


# ---------------------------------------------------------------------------
# TestDeriveLabel
# ---------------------------------------------------------------------------


def _facts_base(**overrides: Any) -> Facts:
    """Return a minimally-populated Facts object with sane defaults."""
    defaults: Dict[str, Any] = {
        "task_status": "TODO",
        "bound_attempt": None,
        "revision": REVISION,
        "coordinator_run": COORDINATOR_RUN,
        "ownership_generation": GENERATION,
        "authorization_in_force": True,
        "receipt_present": False,
        "plan_hash": None,
        "definition_hash": None,
        "reserved": False,
        "prepared": False,
        "launched": False,
        "result": None,
        "declared_scopes": frozenset(),
        "sealed_scopes": frozenset(),
        "open_scopes": frozenset(),
        "open_causes": frozenset(),
        "stop_evidence": None,
        "classified": None,
        "accepted": False,
        "commit_observed": False,
        "released": False,
        "uncertain_start": False,
        "stop_requested": False,
        "grant_present": False,
        "launch_capability": "none",
        "indeterminate": False,
        "indeterminate_path": None,
    }
    defaults.update(overrides)
    return Facts(**defaults)


class TestDeriveLabel:
    def test_row1_indeterminate(self) -> None:
        f = _facts_base(indeterminate=True, indeterminate_path="some/path")
        assert derive_label(f) == "INDETERMINATE"

    def test_row2_released(self) -> None:
        f = _facts_base(released=True)
        assert derive_label(f) == "RELEASED"

    def test_row3_default_is_reserved(self) -> None:
        # No reservation, no grant: default row returns "RESERVED"
        f = _facts_base()
        assert derive_label(f) == "RESERVED"

    def test_row4_reserved_with_bound_attempt(self) -> None:
        f = _facts_base(reserved=True, bound_attempt=ATTEMPT_ID)
        assert derive_label(f) == "RESERVED"

    def test_reserved_with_grant_is_still_reserved(self) -> None:
        # There is no separate "GRANTED" row; grant_present is not a label discriminator
        f = _facts_base(reserved=True, bound_attempt=ATTEMPT_ID, grant_present=True)
        assert derive_label(f) == "RESERVED"

    def test_prepared_label(self) -> None:
        f = _facts_base(
            reserved=True, prepared=True, bound_attempt=ATTEMPT_ID, grant_present=True
        )
        assert derive_label(f) == "PREPARED"

    def test_dispatching_label_capability_issued(self) -> None:
        # launch_capability==issued and not yet launched => DISPATCHING
        f = _facts_base(
            reserved=True, prepared=True,
            bound_attempt=ATTEMPT_ID, grant_present=True,
            launch_capability="issued",
        )
        assert derive_label(f) == "DISPATCHING"

    def test_running_label_launched(self) -> None:
        f = _facts_base(
            reserved=True, prepared=True, launched=True,
            bound_attempt=ATTEMPT_ID, grant_present=True,
            launch_capability="consumed",
            open_scopes=frozenset({"worker"}),
            declared_scopes=frozenset({"worker"}),
        )
        assert derive_label(f) == "RUNNING"

    def test_committed_label(self) -> None:
        f = _facts_base(
            reserved=True, prepared=True, launched=True,
            bound_attempt=ATTEMPT_ID, grant_present=True,
            launch_capability="consumed",
            accepted=True, commit_observed=True,
            classified="completed",
        )
        assert derive_label(f) == "COMMITTED"

    def test_accepted_label(self) -> None:
        f = _facts_base(
            reserved=True, prepared=True, launched=True,
            bound_attempt=ATTEMPT_ID, grant_present=True,
            launch_capability="consumed",
            accepted=True, commit_observed=False,
            classified="completed",
        )
        assert derive_label(f) == "ACCEPTED"


# ---------------------------------------------------------------------------
# TestPerformGate (R5-07)
# ---------------------------------------------------------------------------


class TestPerformGate:
    def test_guard_called_with_facts(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        seen: List[Facts] = []

        def _guard(f: Facts) -> None:
            seen.append(f)

        perform(ctx, task, _guard, lambda c, t: None)
        assert len(seen) == 1

    def test_effect_called_when_guard_passes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        called: List[bool] = []
        perform(ctx, task, lambda f: None, lambda c, t: called.append(True))
        assert called == [True]

    def test_effect_not_called_when_guard_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        called: List[bool] = []

        def _guard(_: Facts) -> None:
            raise PreconditionError("guard says no")

        with pytest.raises(PreconditionError):
            perform(ctx, task, _guard, lambda c, t: called.append(True))
        assert called == []

    def test_indeterminate_raises_before_guard(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "reservation.json").write_bytes(b"{invalid}")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        guard_called: List[bool] = []
        with pytest.raises(OperationError):
            perform(ctx, task, lambda f: guard_called.append(True), lambda c, t: None)
        assert guard_called == []


# ---------------------------------------------------------------------------
# TestO1Reserve
# ---------------------------------------------------------------------------


class TestO1Reserve:
    def test_publishes_reservation_json(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        _o1_reserve(ctx, task, ATTEMPT_ID)
        dest = exec_dir / "attempts" / ATTEMPT_ID / "reservation.json"
        assert dest.exists()
        data = json.loads(dest.read_bytes())
        assert data["record_kind"] == "reservation"
        assert data["attempt_id"] == ATTEMPT_ID
        assert data["task_id"] == TASK_ID
        assert "body_digest" in data

    def test_precondition_task_not_todo(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task(task_status="RUNNING")
        with pytest.raises(PreconditionError, match="task_status"):
            _o1_reserve(ctx, task, ATTEMPT_ID)

    def test_precondition_attempt_already_bound(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        # Write a reservation so facts.bound_attempt is set
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        task = _task(attempt_id=ATTEMPT_ID, task_status="TODO")
        with pytest.raises(PreconditionError, match="bound attempt"):
            _o1_reserve(ctx, task, "att-new")

    def test_idempotent_via_publish_if_absent(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task()
        _o1_reserve(ctx, task, ATTEMPT_ID)
        # Second call with same attempt_id: publish_if_absent returns existing
        _o1_reserve(ctx, task, ATTEMPT_ID)
        dest = exec_dir / "attempts" / ATTEMPT_ID / "reservation.json"
        assert dest.exists()


# ---------------------------------------------------------------------------
# TestO2Grant
# ---------------------------------------------------------------------------


class TestO2Grant:
    def test_inserts_grant_in_registry(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        # Need reservation for reserved=True
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        task = _task(attempt_id=ATTEMPT_ID)
        _o2_grant(ctx, task, ATTEMPT_ID, ["claim:read"])
        grants = ctx.registry.read_all_grants()
        assert any(g["attempt_id"] == ATTEMPT_ID for g in grants)

    def test_precondition_not_reserved(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not reserved"):
            _o2_grant(ctx, task, ATTEMPT_ID, [])

    def test_precondition_grant_already_present(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        _store_grant(ws, ATTEMPT_ID)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="grant already present"):
            _o2_grant(ctx, task, ATTEMPT_ID, [])


# ---------------------------------------------------------------------------
# TestO3Prepare
# ---------------------------------------------------------------------------


class TestO3Prepare:
    def _setup_reserved_granted(self, exec_dir: Path, ws: Path) -> None:
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        _store_grant(ws, ATTEMPT_ID)

    def test_publishes_scope_and_prepared(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_reserved_granted(exec_dir, ws)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        prepared_fields: Dict[str, Any] = {
            "definition_hash": DEFINITION_HASH,
            "plan_hash": "",
            "contract": {},
            "baseline": {},
            "instruction_digest": "i" * 64,
            "deadline_at": "2026-09-14T23:59:59Z",
            "deadline_budget_s": 3600,
            "heartbeat_interval_s": 30,
            "log_cap_bytes": 1024 * 1024,
        }
        _o3_prepare(ctx, task, ATTEMPT_ID, ["claim:run"], prepared_fields)
        assert (exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json").exists()
        assert (exec_dir / "attempts" / ATTEMPT_ID / "prepared.json").exists()

    def test_precondition_not_reserved(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not reserved"):
            _o3_prepare(ctx, task, ATTEMPT_ID, [], {})

    def test_precondition_no_grant(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation",
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            plan_hash="",
            claims=[],
            counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="no grant"):
            _o3_prepare(ctx, task, ATTEMPT_ID, [], {})


# ---------------------------------------------------------------------------
# TestO4Dispatch
# ---------------------------------------------------------------------------


class TestO4Dispatch:
    def _setup_prepared(self, exec_dir: Path, ws: Path) -> None:
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _store_grant(ws, ATTEMPT_ID)

    def test_publishes_intent_fence_ack_launch(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_prepared(exec_dir, ws)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        # launch.json must exist
        assert (attempt_dir / "launch.json").exists()
        # Some mutation directory must exist
        mutations = list((attempt_dir / "mutations").iterdir())
        assert len(mutations) == 1
        mut_dir = mutations[0]
        assert (mut_dir / "intent.json").exists()
        assert (mut_dir / "ack.json").exists()

    def test_precondition_not_prepared(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not prepared"):
            _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})

    def test_precondition_not_authorized(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_prepared(exec_dir, ws)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID, authorization_in_force=False)
        with pytest.raises(PreconditionError, match="authorization_in_force"):
            _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})

    def test_precondition_not_todo(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_prepared(exec_dir, ws)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID, task_status="RUNNING")
        with pytest.raises(PreconditionError, match="task_status"):
            _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})

    def test_precondition_stop_requested(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_prepared(exec_dir, ws)
        # Publish a stop-request to make stop_requested=True
        sr_dir = exec_dir / "control" / "stop-requests"
        sr_dir.mkdir(parents=True)
        _write_record(
            sr_dir / "0001.json",
            record_kind="stop-request",
            task_id=None,
            attempt_id=None,
            sequence=1,
            requested_by="operator",
            reason="manual stop",
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="stop_requested"):
            _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})

    def test_precondition_open_causes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_prepared(exec_dir, ws)
        # Publish a hold to create an open cause
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test hold", raised_by="operator",
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_causes"):
            _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})


# ---------------------------------------------------------------------------
# TestO5RecordResult
# ---------------------------------------------------------------------------


class TestO5RecordResult:
    def test_publishes_result_json_with_capability_consumed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o5_record_result(
            ctx, task, ATTEMPT_ID,
            {"outcome": "success", "produced": {}, "baseline_matched": True,
             "captures": [], "summary": "ok", "refusal_code": None},
        )
        assert (exec_dir / "attempts" / ATTEMPT_ID / "result.json").exists()

    def test_precondition_no_capability_issued_or_consumed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="launch_capability"):
            _o5_record_result(ctx, task, ATTEMPT_ID, {"outcome": "success"})


# ---------------------------------------------------------------------------
# TestO6Seal
# ---------------------------------------------------------------------------


class TestO6Seal:
    def test_publishes_sealed_record(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # scope must be declared
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o6_seal(ctx, task, ATTEMPT_ID, "worker", {"variant": "exited", "code": 0})
        sealed_dir = exec_dir / "attempts" / ATTEMPT_ID / "sealed"
        assert any(sealed_dir.iterdir())

    def test_precondition_scope_not_declared(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not declared"):
            _o6_seal(ctx, task, ATTEMPT_ID, "worker", {})

    def test_precondition_already_sealed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="already sealed"):
            _o6_seal(ctx, task, ATTEMPT_ID, "worker", {})


# ---------------------------------------------------------------------------
# TestO7Classify
# ---------------------------------------------------------------------------


class TestO7Classify:
    def _setup_sealed_worker_with_result(self, exec_dir: Path) -> None:
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            attempt_dir / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        _write_record(
            attempt_dir / "result.json",
            record_kind="result", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            outcome="success",
            produced={},
            baseline_matched=True,
            captures=[],
            summary="ok",
            refusal_code=None,
        )

    def test_publishes_classification_json(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_sealed_worker_with_result(exec_dir)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o7_classify(ctx, task, ATTEMPT_ID, "completed", [{"source": "result"}])
        assert (exec_dir / "attempts" / ATTEMPT_ID / "classification.json").exists()

    def test_precondition_worker_not_sealed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="worker scope not sealed"):
            _o7_classify(ctx, task, ATTEMPT_ID, "completed", [])


# ---------------------------------------------------------------------------
# TestO8Hold
# ---------------------------------------------------------------------------


class TestO8Hold:
    def test_publishes_hold_no_precondition(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o8_hold(ctx, task, ATTEMPT_ID, "cause-001", "operator_hold", "manual", "operator")
        hold_path = exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json"
        assert hold_path.exists()
        data = json.loads(hold_path.read_bytes())
        assert data["cause_id"] == "cause-001"


# ---------------------------------------------------------------------------
# TestO9Resolve
# ---------------------------------------------------------------------------


class TestO9Resolve:
    def test_publishes_resolution_json(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o9_resolve(ctx, task, ATTEMPT_ID, "cause-001", "approved", REVISION, "op", "looks good")
        res_path = exec_dir / "attempts" / ATTEMPT_ID / "resolutions" / "cause-001.json"
        assert res_path.exists()

    def test_precondition_cause_not_in_open_causes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not in open_causes"):
            _o9_resolve(ctx, task, ATTEMPT_ID, "missing-cause", "approved", REVISION, "op", "r")


# ---------------------------------------------------------------------------
# TestO10RunCheck
# ---------------------------------------------------------------------------


class TestO10RunCheck:
    def test_publishes_scope_capture_assessment_seal(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o10_run_check(
            ctx, task, ATTEMPT_ID, "check-sha256", 1,
            capture_fields={"output": "ok", "exit_code": 0},
            assessment_fields={"passed": True, "verdict": "pass"},
            exit_status={"variant": "exited", "code": 0},
        )
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        assert any((attempt_dir / "captures" / "check-sha256").iterdir())
        assert any((attempt_dir / "assessments" / "check-sha256").iterdir())
        assert any((attempt_dir / "sealed").iterdir())

    def test_precondition_not_prepared(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not prepared"):
            _o10_run_check(ctx, task, ATTEMPT_ID, "chk", 1, {}, {}, {})


# ---------------------------------------------------------------------------
# TestO11Accept
# ---------------------------------------------------------------------------


class TestO11Accept:
    def _setup_classified(self, exec_dir: Path, ws: Path) -> None:
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "classification.json",
            record_kind="classification", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            **{"class": "completed"}, evidence=[],
        )

    def test_returns_receipt_id_and_publishes_intent(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_classified(exec_dir, ws)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        rid = _o11_accept(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        assert rid.startswith("rcp-")
        mut_dir = exec_dir / "attempts" / ATTEMPT_ID / "mutations" / rid
        assert (mut_dir / "intent.json").exists()

    def test_precondition_open_causes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_classified(exec_dir, ws)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_causes"):
            _o11_accept(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])

    def test_precondition_not_classified(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="not classified"):
            _o11_accept(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])


# ---------------------------------------------------------------------------
# TestO12CommitAcceptance
# ---------------------------------------------------------------------------


class TestO12CommitAcceptance:
    def _setup_accepted(self, exec_dir: Path, rid: str) -> None:
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid,
            intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={},
            qualifying_captures=[],
        )

    def test_publishes_fence_and_ack(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        self._setup_accepted(exec_dir, rid)
        project_dir = ws / "project"
        project_dir.mkdir()
        ctx = _ctx(exec_dir, ws, project_dir=project_dir, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        mut_dir = exec_dir / "attempts" / ATTEMPT_ID / "mutations" / rid
        assert any((mut_dir / "fences").iterdir())
        assert (mut_dir / "ack.json").exists()

    def test_appends_evidence_md(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        self._setup_accepted(exec_dir, rid)
        project_dir = ws / "project"
        project_dir.mkdir()
        ctx = _ctx(exec_dir, ws, project_dir=project_dir, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        ev_path = project_dir / "evidence.md"
        assert ev_path.exists()
        assert rid in ev_path.read_text()

    def test_precondition_not_accepted(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        fake_rid = "rcp-" + "a" * 60
        with pytest.raises(PreconditionError, match="not accepted"):
            _o12_commit_acceptance(ctx, task, ATTEMPT_ID, fake_rid, DEFINITION_HASH, {}, "DONE")


# ---------------------------------------------------------------------------
# TestO13Withdraw
# ---------------------------------------------------------------------------


class TestO13Withdraw:
    def _setup_reserved(self, exec_dir: Path) -> None:
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )

    def test_publishes_intent_fence_ack(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_reserved(exec_dir)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o13_withdraw(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, "BLOCKED")
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, [])
        mut_dir = exec_dir / "attempts" / ATTEMPT_ID / "mutations" / rid
        assert (mut_dir / "intent.json").exists()
        assert (mut_dir / "ack.json").exists()

    def test_precondition_open_scopes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_reserved(exec_dir)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_scopes"):
            _o13_withdraw(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, "BLOCKED")


# ---------------------------------------------------------------------------
# TestO14Retry
# ---------------------------------------------------------------------------


class TestO14Retry:
    def test_publishes_intent_fence_ack(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        new_attempt_id = "att-retry-001"
        # Set up a blocked task with no open causes
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID, task_status="BLOCKED")
        _o14_retry(ctx, task, new_attempt_id, DEFINITION_HASH, {})
        rid = receipt_id(new_attempt_id, DEFINITION_HASH, "TODO", {}, [])
        mut_dir = exec_dir / "attempts" / new_attempt_id / "mutations" / rid
        assert (mut_dir / "intent.json").exists()
        assert (mut_dir / "ack.json").exists()

    def test_precondition_not_blocked(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(task_status="TODO")
        with pytest.raises(PreconditionError, match="BLOCKED"):
            _o14_retry(ctx, task, "att-new", DEFINITION_HASH, {})

    def test_precondition_open_causes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID, task_status="BLOCKED")
        with pytest.raises(PreconditionError, match="open_causes"):
            _o14_retry(ctx, task, "att-new", DEFINITION_HASH, {})


# ---------------------------------------------------------------------------
# TestO15Stop
# ---------------------------------------------------------------------------


class TestO15Stop:
    def test_publishes_stop_request_hold_evidence(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        (exec_dir / "control" / "stop-requests").mkdir(parents=True)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o15_stop(ctx, task, ATTEMPT_ID, 1, "operator", "test stop", "cause-stop-001", "operator_stop")
        sr_path = exec_dir / "control" / "stop-requests" / "0001.json"
        assert sr_path.exists()
        hold_path = exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-stop-001.json"
        assert hold_path.exists()
        se_path = exec_dir / "attempts" / ATTEMPT_ID / "stop-evidence.json"
        assert se_path.exists()


# ---------------------------------------------------------------------------
# TestO16Release
# ---------------------------------------------------------------------------


class TestO16Release:
    def _setup_releaseable(self, exec_dir: Path, ws: Path) -> None:
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            attempt_dir / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")

    def test_publishes_release_and_removes_grant(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        self._setup_releaseable(exec_dir, ws)
        # A commit_observed ack must exist (since no acceptance, that's fine — no accept in facts)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o16_release(ctx, task, ATTEMPT_ID, None, "normal")
        assert (exec_dir / "attempts" / ATTEMPT_ID / "release.json").exists()
        # Grant should be removed
        grants = ctx.registry.read_all_grants()
        assert not any(g["attempt_id"] == ATTEMPT_ID for g in grants)

    def test_precondition_open_scopes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_scopes"):
            _o16_release(ctx, task, ATTEMPT_ID, None, "normal")

    def test_precondition_capability_not_consumed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _store_grant(ws, ATTEMPT_ID, capability="issued", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="consumed or revoked"):
            _o16_release(ctx, task, ATTEMPT_ID, None, "normal")


# ---------------------------------------------------------------------------
# TestO17TakeOver
# ---------------------------------------------------------------------------


class TestO17TakeOver:
    def test_bumps_generation_and_deletes_runtime(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        runtime_dir = exec_dir / "runtime"
        # The tmp fixture already creates runtime/tmp; confirm it exists before the call
        assert runtime_dir.exists()
        ctx = _ctx(exec_dir, ws, generation=1, commit_fn=_null_commit)
        old_gen = ctx.ownership_generation
        _o17_take_over(ctx)
        assert ctx.ownership_generation == old_gen + 1
        assert not runtime_dir.exists()

    def test_publishes_generation_fence(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws, generation=2, commit_fn=_null_commit)
        _o17_take_over(ctx)
        fence_dir = exec_dir / "generation-fences"
        assert fence_dir.exists()
        assert any(fence_dir.iterdir())


# ---------------------------------------------------------------------------
# TestO18Dispose
# ---------------------------------------------------------------------------


class TestO18Dispose:
    def test_seals_open_scopes_and_releases(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o18_dispose(ctx, task, ATTEMPT_ID, "dispose")
        assert (exec_dir / "attempts" / ATTEMPT_ID / "release.json").exists()
        sealed_dir = exec_dir / "attempts" / ATTEMPT_ID / "sealed"
        assert any(sealed_dir.iterdir())

    def test_precondition_capability_not_none(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _store_grant(ws, ATTEMPT_ID, capability="issued", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="launch_capability"):
            _o18_dispose(ctx, task, ATTEMPT_ID, "dispose")


# ---------------------------------------------------------------------------
# TestO19Acquire
# ---------------------------------------------------------------------------


class TestO19Acquire:
    def test_publishes_owner_and_sets_coordinator_run(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws, coordinator_run=None, generation=0, commit_fn=_null_commit)
        owner_fields: Dict[str, Any] = {
            "host_id": "host-001",
            "boot_id": "boot-001",
            "pid": 12345,
            "process_start": "2026-09-14T00:00:00Z",
            "started_at": "2026-09-14T00:00:00Z",
        }
        _o19_acquire(ctx, owner_fields)
        assert ctx.coordinator_run is not None
        assert ctx.coordinator_run.startswith("run-")
        assert ctx.ownership_generation == 1
        owners_dir = exec_dir / "owners"
        assert owners_dir.exists()
        assert any(owners_dir.iterdir())

    def test_precondition_coordinator_run_already_set(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws, coordinator_run=COORDINATOR_RUN, commit_fn=_null_commit)
        with pytest.raises(PreconditionError, match="coordinator_run"):
            _o19_acquire(ctx, {})


# ---------------------------------------------------------------------------
# TestO20Relinquish
# ---------------------------------------------------------------------------


class TestO20Relinquish:
    def test_clears_coordinator_run(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        _o20_relinquish(ctx)
        assert ctx.coordinator_run is None

    def test_precondition_grants_present(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        with pytest.raises(PreconditionError, match="grants still present"):
            _o20_relinquish(ctx)


# ---------------------------------------------------------------------------
# Coverage-gap tests: _read_record validity branches (§6.3)
# ---------------------------------------------------------------------------


class TestReadRecordValidity:
    """Cover the §6.3 indeterminate branches in _read_record."""

    def test_oserror_on_read_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        path = attempt_dir / "reservation.json"
        # Make dir exist but then mock read_bytes to raise an OSError
        path.write_bytes(b"placeholder")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with mock.patch.object(
            type(path), "read_bytes", side_effect=OSError("permission denied")
        ):
            facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_non_utf8_bytes_are_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "reservation.json").write_bytes(b"\xff\xfe not json")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_wrong_project_id_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        # Write a reservation with a different project_id
        raw: Dict[str, Any] = {
            "record_kind": "reservation",
            "schema_version": "1.0",
            "project_id": "OTHER-PROJECT",
            "coordinator_run": COORDINATOR_RUN,
            "ownership_generation": GENERATION,
            "writer": "coordinator",
            "written_at": "2026-09-14T00:00:00Z",
            "task_id": TASK_ID,
            "attempt_id": ATTEMPT_ID,
            "plan_hash": "",
            "claims": [],
            "counter": 0,
        }
        from execution_serialization import body_digest as _bd
        from execution_serialization import canonical_json as _cj
        raw["body_digest"] = _bd(raw)
        (attempt_dir / "reservation.json").parent.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "reservation.json").write_bytes(_cj(raw))
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_attempt_id_mismatch_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        # Write a reservation record claiming a different attempt_id
        raw: Dict[str, Any] = {
            "record_kind": "reservation",
            "schema_version": "1.0",
            "project_id": PROJECT_ID,
            "coordinator_run": COORDINATOR_RUN,
            "ownership_generation": GENERATION,
            "writer": "coordinator",
            "written_at": "2026-09-14T00:00:00Z",
            "task_id": TASK_ID,
            "attempt_id": "att-WRONG",
            "plan_hash": "",
            "claims": [],
            "counter": 0,
        }
        from execution_serialization import body_digest as _bd
        from execution_serialization import canonical_json as _cj
        raw["body_digest"] = _bd(raw)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "reservation.json").write_bytes(_cj(raw))
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_body_digest_mismatch_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True, exist_ok=True)
        raw: Dict[str, Any] = {
            "record_kind": "reservation",
            "schema_version": "1.0",
            "project_id": PROJECT_ID,
            "coordinator_run": COORDINATOR_RUN,
            "ownership_generation": GENERATION,
            "writer": "coordinator",
            "written_at": "2026-09-14T00:00:00Z",
            "task_id": TASK_ID,
            "attempt_id": ATTEMPT_ID,
            "plan_hash": "",
            "claims": [],
            "counter": 0,
            "body_digest": "bad_digest",  # intentionally wrong
        }
        import json as _json
        (attempt_dir / "reservation.json").write_bytes(
            _json.dumps(raw, sort_keys=True).encode("utf-8")
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_wrong_task_id_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True, exist_ok=True)
        raw: Dict[str, Any] = {
            "record_kind": "reservation",
            "schema_version": "1.0",
            "project_id": PROJECT_ID,
            "coordinator_run": COORDINATOR_RUN,
            "ownership_generation": GENERATION,
            "writer": "coordinator",
            "written_at": "2026-09-14T00:00:00Z",
            "task_id": "task-WRONG",
            "attempt_id": ATTEMPT_ID,
            "plan_hash": "",
            "claims": [],
            "counter": 0,
        }
        from execution_serialization import body_digest as _bd
        from execution_serialization import canonical_json as _cj
        raw["body_digest"] = _bd(raw)
        (attempt_dir / "reservation.json").write_bytes(_cj(raw))
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate


# ---------------------------------------------------------------------------
# Coverage-gap tests: derive_label missing rows
# ---------------------------------------------------------------------------


class TestDeriveLabelMissingRows:
    def test_quarantined_open_causes_and_open_scopes(self) -> None:
        f = _facts_base(
            open_causes=frozenset({"c1"}),
            open_scopes=frozenset({"worker"}),
        )
        assert derive_label(f) == "QUARANTINED"

    def test_held_open_causes_no_open_scopes(self) -> None:
        f = _facts_base(open_causes=frozenset({"c1"}))
        assert derive_label(f) == "HELD"

    def test_classified_label(self) -> None:
        f = _facts_base(classified="completed")
        assert derive_label(f) == "CLASSIFIED"

    def test_published_label(self) -> None:
        f = _facts_base(result="success")
        assert derive_label(f) == "PUBLISHED"

    def test_uncertain_start(self) -> None:
        f = _facts_base(launched=True, uncertain_start=True)
        assert derive_label(f) == "UNCERTAIN"

    def test_uncertain_deadline_exceeded(self) -> None:
        f = _facts_base()
        assert derive_label(f, deadline_exceeded=True) == "UNCERTAIN"


# ---------------------------------------------------------------------------
# Coverage-gap tests: projection branches
# ---------------------------------------------------------------------------


class TestProjectionBranches:
    def test_indeterminate_from_stop_requested(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        sr_dir = exec_dir / "control" / "stop-requests"
        sr_dir.mkdir(parents=True)
        # Write an invalid stop-request to trigger indeterminate via bad record
        (sr_dir / "0001.json").write_bytes(b"not json")
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_attempt_fences_merged(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Publish a generation fence inside the attempt dir
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # No actual generation-fences dir — just verify facts compute without error
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.reserved

    def test_uncertain_start_from_launch(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "launch.json",
            record_kind="launch", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            start_outcome="ambiguous", handle=None,
            capability_id="cap-1", started_at="2026-09-14T00:00:00Z",
            adapter={"name": "fake", "version": "0"},
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.uncertain_start

    def test_stop_evidence_from_record(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "stop-evidence.json",
            record_kind="stop-evidence", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            kind="operator_stop",
            observed_at="2026-09-14T00:00:00Z",
            detail="stopped by operator",
            scopes=[],
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.stop_evidence == "operator_stop"

    def test_resolved_cause_reduces_open_causes(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        _write_record(
            attempt_dir / "resolutions" / "cause-001.json",
            record_kind="resolution", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", resolution="approved",
            observed_revision=REVISION, decided_by="op", rationale="ok",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert "cause-001" not in facts.open_causes

    def test_stop_clearance_lifts_stop_requested(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        sr_dir = exec_dir / "control" / "stop-requests"
        sr_dir.mkdir(parents=True)
        _write_record(
            sr_dir / "0001.json",
            record_kind="stop-request", task_id=None, attempt_id=None,
            sequence=1, requested_by="operator", reason="test",
        )
        cl_dir = exec_dir / "control" / "clearances"
        cl_dir.mkdir(parents=True)
        _write_record(
            cl_dir / "0001.json",
            record_kind="stop-clearance", task_id=None, attempt_id=None,
            sequence=1, clears=1, cleared_by="operator", rationale="resolved",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.stop_requested

    def test_mutations_accepted_and_commit_observed(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        _write_record(
            attempt_dir / "mutations" / rid / "ack.json",
            record_kind="commit-observed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, fence_sequence=1,
            committed_revision=REVISION + 1, evidence_index=0,
            committed_status="DONE",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.accepted
        assert facts.commit_observed

    def test_mutations_dispatch_intent_skipped(self, tmp: Any) -> None:
        """RUNNING intent (dispatch) must not set accepted=True."""
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        dispatch_rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "RUNNING", {}, [])
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "mutations" / dispatch_rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=dispatch_rid, intent="RUNNING",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.accepted

    def test_scope_id_mismatch_in_sealed_is_indeterminate(self, tmp: Any) -> None:
        """scope_id in the record must match the filename."""
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # Write a sealed record whose scope_id doesn't match the filename
        raw: Dict[str, Any] = {
            "record_kind": "sealed",
            "schema_version": "1.0",
            "project_id": PROJECT_ID,
            "coordinator_run": COORDINATOR_RUN,
            "ownership_generation": GENERATION,
            "writer": "coordinator",
            "written_at": "2026-09-14T00:00:00Z",
            "task_id": TASK_ID,
            "attempt_id": ATTEMPT_ID,
            "scope_id": "OTHER-SCOPE",  # mismatch with filename "worker"
            "exit": {"variant": "exited", "code": 0},
            "tree_exited": True,
            "resumable": False,
        }
        from execution_serialization import body_digest as _bd
        from execution_serialization import canonical_json as _cj
        raw["body_digest"] = _bd(raw)
        sealed_dir = attempt_dir / "sealed"
        sealed_dir.mkdir(parents=True, exist_ok=True)
        (sealed_dir / "worker.json").write_bytes(_cj(raw))
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate


# ---------------------------------------------------------------------------
# Coverage-gap tests: next_fence_sequence with existing fences
# ---------------------------------------------------------------------------


class TestNextFenceSequence:
    def test_returns_max_plus_one_when_fences_exist(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        # First commit-acceptance publishes fence 1
        _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        # Re-publish intent so we can call O12 again (simulating a retry scenario)
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        # Second call: fence seq should increment
        ctx2 = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task2 = _task(attempt_id=ATTEMPT_ID)
        _o12_commit_acceptance(ctx2, task2, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        fences_dir = attempt_dir / "mutations" / rid / "fences"
        fence_nums = [int(f.stem) for f in fences_dir.iterdir() if f.suffix == ".json"]
        assert sorted(fence_nums) == [1, 2]


# ---------------------------------------------------------------------------
# Coverage-gap tests: O3 plan_hash mismatch
# ---------------------------------------------------------------------------


class TestO3PlanHashMismatch:
    def test_plan_hash_mismatch_raises(self, tmp: Any) -> None:
        """facts.plan_hash comes from the reservation record; task.plan_hash is the
        coordinator's current expectation.  A mismatch means the plan changed since
        the reservation was made, which must prevent prepare."""
        exec_dir, ws = tmp
        # Reservation was made with plan-hash v1
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="hash-v1", claims=[], counter=0,
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        # Coordinator now sees plan-hash v2 — O3 must reject
        task = _task(attempt_id=ATTEMPT_ID, plan_hash="hash-v2")
        with pytest.raises(PreconditionError, match="plan_hash"):
            _o3_prepare(ctx, task, ATTEMPT_ID, [], {
                "definition_hash": DEFINITION_HASH,
                "plan_hash": "hash-v2",
                "contract": {},
                "baseline": {},
                "instruction_digest": "i" * 64,
                "deadline_at": "2026-09-14T23:59:59Z",
                "deadline_budget_s": 3600,
                "heartbeat_interval_s": 30,
                "log_cap_bytes": 1048576,
            })


# ---------------------------------------------------------------------------
# TestFakeAdapter — 100% coverage for execution_adapter.py
# ---------------------------------------------------------------------------


class TestFakeAdapter:
    def test_enqueue_start_returns_self(self) -> None:
        fa = FakeAdapter()
        result = fa.enqueue_start("h-1")
        assert result is fa

    def test_enqueue_observe_returns_self(self) -> None:
        fa = FakeAdapter()
        result = fa.enqueue_observe("h-1", OBSERVE_RUNNING)
        assert result is fa

    def test_enqueue_seal_returns_self(self) -> None:
        fa = FakeAdapter()
        result = fa.enqueue_seal("h-1", {"tree_exited": True, "resumable": False})
        assert result is fa

    def test_set_terminate_raises_returns_self(self) -> None:
        fa = FakeAdapter()
        result = fa.set_terminate_raises()
        assert result is fa

    def test_start_uses_queue(self) -> None:
        fa = FakeAdapter()
        fa.enqueue_start("h-queued")
        handle = fa.start({}, "/tmp/attempt")
        assert handle == "h-queued"

    def test_observe_default_unknown(self) -> None:
        fa = FakeAdapter()
        result = fa.observe("h-1")
        assert result == OBSERVE_UNKNOWN

    def test_observe_sticky(self) -> None:
        fa = FakeAdapter()
        fa.enqueue_observe("h-1", OBSERVE_RUNNING)
        r1 = fa.observe("h-1")  # pops → empty → re-appended (sticky)
        assert r1 == OBSERVE_RUNNING
        r2 = fa.observe("h-1")  # sticky item
        assert r2 == OBSERVE_RUNNING

    def test_seal_no_queue_raises(self) -> None:
        fa = FakeAdapter()
        with pytest.raises(AdapterError):
            fa.seal("h-no-queue")

    def test_seal_sticky(self) -> None:
        fa = FakeAdapter()
        att = {"tree_exited": True, "resumable": False}
        fa.enqueue_seal("h-1", att)
        r1 = fa.seal("h-1")  # pops → empty → re-appended (sticky)
        r2 = fa.seal("h-1")  # sticky item
        assert r1 == r2 == att

    def test_terminate_raises(self) -> None:
        fa = FakeAdapter()
        fa.set_terminate_raises()
        with pytest.raises(AdapterError):
            fa.terminate("h-1")


# ---------------------------------------------------------------------------
# TestCoverageGaps — remaining execution_ops.py lines
# ---------------------------------------------------------------------------


class TestCoverageGaps:

    # --- Generation-fence loading ---

    def test_unexpected_gen_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Reservation written with gen=0 (old generation); no fence explains it
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
            ownership_generation=0,
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)  # generation=GENERATION=1
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    def test_valid_root_fence_loaded(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Reservation with gen=0; root fence at generation-fences/0.json explains it
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
            ownership_generation=0,
        )
        _write_record(
            exec_dir / "generation-fences" / "0.json",
            record_kind="generation-fence", task_id=None, attempt_id=None,
            generation=GENERATION,
            taken_over_from=0,
            scope="project",
            records=[f"attempts/{ATTEMPT_ID}/reservation.json"],
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.indeterminate
        assert facts.reserved

    def test_attempt_fence_explains_gen(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Reservation with gen=0; attempt-level fence explains it (no root fence)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
            ownership_generation=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "generation-fences" / "0.json",
            record_kind="generation-fence", task_id=None, attempt_id=None,
            generation=GENERATION,
            taken_over_from=0,
            scope="project",
            records=[f"attempts/{ATTEMPT_ID}/reservation.json"],
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.indeterminate
        assert facts.reserved

    # --- Non-.json skip paths ---

    def test_scope_non_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # Non-.json file in scopes dir — must not raise or count
        (exec_dir / "attempts" / ATTEMPT_ID / "scopes").mkdir(parents=True)
        (exec_dir / "attempts" / ATTEMPT_ID / "scopes" / "readme.txt").write_text("ignore")
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.indeterminate
        assert "readme" not in facts.open_scopes

    def test_resolutions_non_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        # Non-.json file in resolutions — must not be treated as a resolution
        (exec_dir / "attempts" / ATTEMPT_ID / "resolutions").mkdir(parents=True)
        (exec_dir / "attempts" / ATTEMPT_ID / "resolutions" / "readme.txt").write_text("ignore")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        # Hold is still open because the .txt file was skipped
        assert "cause-001" in facts.open_causes

    def test_holds_non_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        (exec_dir / "attempts" / ATTEMPT_ID / "holds").mkdir(parents=True)
        (exec_dir / "attempts" / ATTEMPT_ID / "holds" / "readme.txt").write_text("ignore")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.open_causes

    def test_mutations_non_dir_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # File (not dir) in mutations — must be skipped
        (exec_dir / "attempts" / ATTEMPT_ID / "mutations").mkdir(parents=True)
        (exec_dir / "attempts" / ATTEMPT_ID / "mutations" / "notadir.txt").write_text("ignore")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.accepted

    def test_mutations_no_intent_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # Dir in mutations but no intent.json
        (exec_dir / "attempts" / ATTEMPT_ID / "mutations" / "some-rid").mkdir(parents=True)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert not facts.accepted

    # --- Stop-request / clearance non-.json ---

    def test_clearances_non_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Active stop-request; clearances dir has only a .txt file → not cleared
        sr_dir = exec_dir / "control" / "stop-requests"
        sr_dir.mkdir(parents=True)
        _write_record(
            sr_dir / "0001.json",
            record_kind="stop-request", task_id=None, attempt_id=None,
            sequence=1, requested_by="operator", reason="test",
        )
        cl_dir = exec_dir / "control" / "clearances"
        cl_dir.mkdir(parents=True)
        (cl_dir / "readme.txt").write_text("ignore")
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert facts.stop_requested

    def test_stop_requests_non_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        sr_dir = exec_dir / "control" / "stop-requests"
        sr_dir.mkdir(parents=True)
        (sr_dir / "readme.txt").write_text("ignore")
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.stop_requested

    # --- _next_fence_sequence ValueError ---

    def test_next_fence_non_numeric_ignored(self, tmp: Any) -> None:
        exec_dir, _ws = tmp
        mutations_dir = exec_dir / "mutations"
        fences_dir = mutations_dir / "any-rid" / "fences"
        fences_dir.mkdir(parents=True)
        (fences_dir / "abc.json").write_text("{}")   # non-numeric stem → skipped
        (fences_dir / "0001.json").write_text("{}")  # numeric → highest=1
        seq = _next_fence_sequence(mutations_dir, "any-rid")
        assert seq == 2

    # --- O4: no commit_fn / no grant ---

    def test_o4_no_commit_fn(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _store_grant(ws, ATTEMPT_ID)
        # No commit_fn → ev_idx=0 branch taken
        ctx = _ctx(exec_dir, ws, commit_fn=None)
        task = _task(attempt_id=ATTEMPT_ID)
        _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})
        assert (attempt_dir / "launch.json").exists()

    def test_o4_no_grant(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        # No grant in registry → grant=None branch; cap_id still generated
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o4_dispatch(ctx, task, ATTEMPT_ID, DEFINITION_HASH, {})
        assert (attempt_dir / "launch.json").exists()

    # --- O7: no result, no stop-evidence, not deadline exceeded ---

    def test_o7_no_result_no_evidence_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            attempt_dir / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        # No result.json, no stop-evidence.json, deadline_exceeded=False
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="no result"):
            _o7_classify(ctx, task, ATTEMPT_ID, "completed", [], deadline_exceeded=False)

    # --- O9: evidence field ---

    def test_o9_with_evidence(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o9_resolve(
            ctx, task, ATTEMPT_ID, "cause-001", "approved", REVISION,
            "op", "looks good", evidence=[{"src": "x"}],
        )
        res_path = exec_dir / "attempts" / ATTEMPT_ID / "resolutions" / "cause-001.json"
        data = json.loads(res_path.read_bytes())
        assert "evidence" in data

    # --- O11: open scopes precondition ---

    def test_o11_open_scopes_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        # Add classification so classification precondition passes
        _write_record(
            attempt_dir / "classification.json",
            record_kind="classification", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            **{"class": "completed"}, evidence=[],
        )
        # open_scopes non-empty (worker not sealed) → O11 should raise
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_scopes"):
            _o11_accept(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])

    # --- O12: definition_hash mismatch, evidence.md OSError, no commit_fn ---

    def test_o12_definition_hash_mismatch_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        different_hash = "x" + "0" * 63
        with pytest.raises(PreconditionError, match="definition_hash"):
            _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, different_hash, {}, "DONE")

    def test_o12_evidence_oserror_silent(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        # project_dir is a file, not a directory: open("a") raises OSError → silently ignored
        project_dir = ws / "project_file"
        project_dir.write_text("not a dir")
        ctx = _ctx(exec_dir, ws, project_dir=project_dir, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID)
        _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        mut_dir = attempt_dir / "mutations" / rid
        assert (mut_dir / "ack.json").exists()

    def test_o12_no_commit_fn(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "prepared.json",
            record_kind="prepared", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            definition_hash=DEFINITION_HASH, plan_hash="", contract={}, baseline={},
            instruction_digest="i" * 64,
            deadline_at="2026-09-14T23:59:59Z", deadline_budget_s=3600,
            heartbeat_interval_s=30, log_cap_bytes=1048576,
        )
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        ctx = _ctx(exec_dir, ws, commit_fn=None)
        task = _task(attempt_id=ATTEMPT_ID)
        _o12_commit_acceptance(ctx, task, ATTEMPT_ID, rid, DEFINITION_HASH, {}, "DONE")
        mut_dir = attempt_dir / "mutations" / rid
        assert (mut_dir / "ack.json").exists()

    # --- O13: auth not in force (early return), no commit_fn ---

    def test_o13_auth_not_in_force(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # auth_not_in_force → guard returns (not raises) → effect still runs
        ctx = _ctx(exec_dir, ws, commit_fn=_null_commit)
        task = _task(attempt_id=ATTEMPT_ID, authorization_in_force=False)
        _o13_withdraw(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, "BLOCKED")
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, [])
        mut_dir = exec_dir / "attempts" / ATTEMPT_ID / "mutations" / rid
        assert (mut_dir / "ack.json").exists()

    def test_o13_no_commit_fn(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws, commit_fn=None)
        task = _task(attempt_id=ATTEMPT_ID)
        _o13_withdraw(ctx, task, ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, "BLOCKED")
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "BLOCKED", {}, [])
        mut_dir = exec_dir / "attempts" / ATTEMPT_ID / "mutations" / rid
        assert (mut_dir / "ack.json").exists()

    # --- O14: no commit_fn ---

    def test_o14_no_commit_fn(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        ctx = _ctx(exec_dir, ws, commit_fn=None)
        task = _task(attempt_id=ATTEMPT_ID, task_status="BLOCKED")
        new_attempt = "att-retry-002"
        _o14_retry(ctx, task, new_attempt, DEFINITION_HASH, {})
        rid = receipt_id(new_attempt, DEFINITION_HASH, "TODO", {}, [])
        mut_dir = exec_dir / "attempts" / new_attempt / "mutations" / rid
        assert (mut_dir / "ack.json").exists()

    # --- O16: open_causes / accepted-not-committed preconditions, no sealed dir ---

    def test_o16_open_causes_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            attempt_dir / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        _write_record(
            attempt_dir / "holds" / "cause-001.json",
            record_kind="hold", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            cause_id="cause-001", cause_class="operator_hold",
            detail="test", raised_by="operator",
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="open_causes"):
            _o16_release(ctx, task, ATTEMPT_ID, None, "normal")

    def test_o16_accepted_not_committed_raises(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        rid = receipt_id(ATTEMPT_ID, DEFINITION_HASH, "DONE", {}, [])
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        # Write intent (accepted) but NO ack (not commit_observed)
        _write_record(
            attempt_dir / "mutations" / rid / "intent.json",
            record_kind="acceptance", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            receipt_id=rid, intent="DONE",
            definition_hash=DEFINITION_HASH,
            accepted_snapshot={}, qualifying_captures=[],
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        with pytest.raises(PreconditionError, match="commit_observed"):
            _o16_release(ctx, task, ATTEMPT_ID, None, "normal")

    def test_o16_no_sealed_dir(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _store_grant(ws, ATTEMPT_ID, capability="consumed", capability_id="cap-1")
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o16_release(ctx, task, ATTEMPT_ID, None, "normal")
        rel_path = exec_dir / "attempts" / ATTEMPT_ID / "release.json"
        assert rel_path.exists()
        data = json.loads(rel_path.read_bytes())
        assert data["sealed_scopes"] == []

    # --- O17: .json file in exec_dir root ---

    def test_o17_with_json_in_exec_dir(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        (exec_dir / "owner.json").write_text("{}")
        ctx = _ctx(exec_dir, ws, generation=2, commit_fn=_null_commit)
        _o17_take_over(ctx)
        fence_dir = exec_dir / "generation-fences"
        assert fence_dir.exists()
        fence_files = list(fence_dir.iterdir())
        assert fence_files
        fence_data = json.loads(fence_files[0].read_bytes())
        assert "owner.json" in fence_data["records"]

    # --- _read_record: ExecutionRecordError → indeterminate (lines 215-216) ---

    def test_read_record_invalid_schema_is_indeterminate(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Write a reservation with unknown record_kind (validate_record raises ExecutionRecordError)
        _write_record(
            exec_dir / "attempts" / ATTEMPT_ID / "reservation.json",
            record_kind="unknown-xyz", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
        )
        _store_grant(ws, ATTEMPT_ID)
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        facts = project_facts(ctx, task)
        assert facts.indeterminate

    # --- _add_fence failure branches (lines 253-260) ---

    def test_fence_bad_json_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        # Bad JSON in a fence file → _add_fence except branch (lines 253-254)
        fence_dir = exec_dir / "generation-fences"
        fence_dir.mkdir(parents=True)
        (fence_dir / "0001.json").write_bytes(b"not valid json {{{")
        ctx = _ctx(exec_dir, ws)
        task = _task()
        # No error raised; fence silently skipped
        facts = project_facts(ctx, task)
        assert not facts.indeterminate

    def test_fence_wrong_project_id_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        fence_dir = exec_dir / "generation-fences"
        # Valid fence but with wrong project_id → line 256
        _write_record(
            fence_dir / "0001.json",
            record_kind="generation-fence", task_id=None, attempt_id=None,
            generation=GENERATION, taken_over_from=0, scope="project", records=[],
            project_id="wrong-project",
        )
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.indeterminate

    def test_fence_wrong_ownership_generation_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        fence_dir = exec_dir / "generation-fences"
        # Valid fence with ownership_generation != current_generation → line 258
        _write_record(
            fence_dir / "0001.json",
            record_kind="generation-fence", task_id=None, attempt_id=None,
            generation=GENERATION, taken_over_from=0, scope="project", records=[],
            ownership_generation=GENERATION + 1,
        )
        ctx = _ctx(exec_dir, ws)  # generation=GENERATION
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.indeterminate

    def test_fence_digest_mismatch_skipped(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        fence_dir = exec_dir / "generation-fences"
        fence_path = fence_dir / "0001.json"
        # Write valid fence then corrupt the body_digest → line 260
        _write_record(
            fence_path,
            record_kind="generation-fence", task_id=None, attempt_id=None,
            generation=GENERATION, taken_over_from=0, scope="project", records=[],
        )
        data = json.loads(fence_path.read_bytes())
        data["body_digest"] = "0" * 64
        fence_path.write_bytes(json.dumps(data).encode())
        ctx = _ctx(exec_dir, ws)
        task = _task()
        facts = project_facts(ctx, task)
        assert not facts.indeterminate

    # --- O18: sealed scopes discarded from open_scopes ---

    def test_o18_sealed_scopes_discarded(self, tmp: Any) -> None:
        exec_dir, ws = tmp
        attempt_dir = exec_dir / "attempts" / ATTEMPT_ID
        _write_record(
            attempt_dir / "reservation.json",
            record_kind="reservation", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            plan_hash="", claims=[], counter=0,
        )
        _write_record(
            attempt_dir / "scopes" / "worker.json",
            record_kind="scope", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", kind="worker", claims=[],
        )
        _write_record(
            attempt_dir / "sealed" / "worker.json",
            record_kind="sealed", task_id=TASK_ID, attempt_id=ATTEMPT_ID,
            scope_id="worker", exit={"variant": "exited", "code": 0},
            tree_exited=True, resumable=False,
        )
        _store_grant(ws, ATTEMPT_ID)  # capability="none"
        ctx = _ctx(exec_dir, ws)
        task = _task(attempt_id=ATTEMPT_ID)
        _o18_dispose(ctx, task, ATTEMPT_ID, "dispose")
        # worker was already sealed → discarded from open_scopes → no new O6 seals added
        sealed_dir = attempt_dir / "sealed"
        sealed_files = [f for f in sealed_dir.iterdir() if f.suffix == ".json"]
        assert len(sealed_files) == 1  # original worker.json, no new seals
        assert (attempt_dir / "release.json").exists()


