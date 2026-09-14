"""Canonical serialization, digests, id validation, and record schemas for the execution store.

Comparison keys arrive already resolved; this module never reads the filesystem, resolves paths,
or probes volumes. All digest functions operate on dicts already parsed from JSON.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExecutionIdError(ValueError):
    """Raised when an id or claim string fails validation."""


class ExecutionRecordError(ValueError):
    """Raised when a raw dict fails record schema validation."""


# ---------------------------------------------------------------------------
# Canonical serialization (§7.3)
# ---------------------------------------------------------------------------

_BODY_STRIP = frozenset({"body_digest", "receipt_id"})
_CONTENT_STRIP = frozenset(
    {"body_digest", "receipt_id", "coordinator_run", "ownership_generation", "writer", "written_at"}
)


def canonical_json(obj: object) -> bytes:
    """Return the canonical JSON serialization: sorted keys, no whitespace, UTF-8, trailing newline."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def body_digest(record: Dict[str, Any]) -> str:
    """sha256 of canonical_json(record) with 'body_digest' and 'receipt_id' keys removed."""
    stripped = {k: v for k, v in record.items() if k not in _BODY_STRIP}
    return hashlib.sha256(canonical_json(stripped)).hexdigest()


def content_digest(record: Dict[str, Any]) -> str:
    """sha256 of canonical_json(record) with body_digest, receipt_id, and writer-stamped fields removed."""
    stripped = {k: v for k, v in record.items() if k not in _CONTENT_STRIP}
    return hashlib.sha256(canonical_json(stripped)).hexdigest()


def capture_id(capture_record: Dict[str, Any]) -> str:
    """'cap-' + first 32 characters of body_digest(capture_record)."""
    return "cap-" + body_digest(capture_record)[:32]


def receipt_id(
    attempt_id: str,
    definition_hash: str,
    intent: str,
    accepted_snapshot: Dict[str, Any],
    qualifying_captures: List[Any],
) -> str:
    """'rcp-' + 32 hex chars of sha256 over canonical_json of the identity dict."""
    identity = {
        "attempt_id": attempt_id,
        "definition_hash": definition_hash,
        "intent": intent,
        "accepted_snapshot": accepted_snapshot,
        "qualifying_captures": qualifying_captures,
    }
    return "rcp-" + hashlib.sha256(canonical_json(identity)).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Scope-id encoding (§7.3)
# ---------------------------------------------------------------------------


def encode_scope_id(scope_id: str) -> str:
    """Encode a scope_id for use as a filename component: ':' -> '%3A', '#' -> '%23'."""
    return scope_id.replace(":", "%3A").replace("#", "%23")


def decode_scope_id(encoded: str) -> str:
    """Reverse of encode_scope_id."""
    return encoded.replace("%3A", ":").replace("%23", "#")


# ---------------------------------------------------------------------------
# Id validation (§7.3)
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def validate_id(value: object, field: str) -> str:
    """Validate that value is a non-empty string with only safe path-segment characters.

    Accepts ASCII alphanumeric, '-', '_', '.', '~'. Rejects non-strings, empty strings,
    and any character outside that set (including '/', ':', '#', NUL, whitespace).
    """
    if not isinstance(value, str):
        raise ExecutionIdError(f"{field} must be str, got {type(value).__name__!r}: {value!r}")
    if not value:
        raise ExecutionIdError(f"{field} must not be empty")
    if not _ID_RE.match(value):
        raise ExecutionIdError(f"{field} contains characters invalid in a path segment: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Cause classification (§14.1)
# ---------------------------------------------------------------------------

CAUSE_CLASSES: FrozenSet[str] = frozenset(
    {
        "check_failure",
        "indeterminate_check",
        "flaky_check",
        "plan_defect",
        "stale_evidence",
        "definition_changed",
        "conflicting_publication",
        "corrupt_record",
        "store_error",
        "authorization_lost",
        "lost_contact",
        "human_review",
        "operator_stop",
        "unsafe_release",
    }
)

CLASSIFICATION_CLASSES: FrozenSet[str] = frozenset(
    {
        "success",
        "check_failure",
        "indeterminate_execution",
        "no_result",
        "stopped",
        "partial_write",
        "plan_defect",
        "refused",
    }
)


def validate_cause_class(value: object) -> str:
    """Validate value is one of CAUSE_CLASSES. Raises ExecutionIdError on failure."""
    if not isinstance(value, str) or value not in CAUSE_CLASSES:
        raise ExecutionIdError(f"unknown cause class: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Plan-schema claim mode encoding (§9.2)
# ---------------------------------------------------------------------------

_VALID_NAMESPACES = frozenset({"local", "store"})
_VALID_ACCESSES = frozenset({"read", "write"})


def parse_claim_string(s: object) -> Tuple[str, str, str]:
    """Parse a claim string '<namespace>:<key> <access>' into (namespace, key, access).

    Raises ExecutionIdError if malformed.
    """
    if not isinstance(s, str):
        raise ExecutionIdError(f"claim string must be str, got {type(s).__name__!r}: {s!r}")
    if " " not in s:
        raise ExecutionIdError(f"claim string missing space separator: {s!r}")
    body, access = s.rsplit(" ", 1)
    if access not in _VALID_ACCESSES:
        raise ExecutionIdError(f"claim string has unknown access {access!r}: {s!r}")
    if ":" not in body:
        raise ExecutionIdError(f"claim string missing namespace separator: {s!r}")
    namespace, key = body.split(":", 1)
    if namespace not in _VALID_NAMESPACES:
        raise ExecutionIdError(f"claim string has unknown namespace {namespace!r}: {s!r}")
    if not key:
        raise ExecutionIdError(f"claim string key must not be empty: {s!r}")
    return namespace, key, access


def format_claim_string(namespace: str, key: str, access: str) -> str:
    """Format (namespace, key, access) into a '<namespace>:<key> <access>' claim string."""
    if namespace not in _VALID_NAMESPACES:
        raise ExecutionIdError(f"unknown namespace: {namespace!r}")
    if access not in _VALID_ACCESSES:
        raise ExecutionIdError(f"unknown access: {access!r}")
    if not key:
        raise ExecutionIdError("key must not be empty")
    return f"{namespace}:{key} {access}"


# ---------------------------------------------------------------------------
# Common and attempt envelopes (§7.4)
# ---------------------------------------------------------------------------

_VALID_WRITERS = frozenset({"coordinator", "worker", "adapter"})


@dataclass(frozen=True)
class CommonEnvelope:
    record_kind: str
    schema_version: str
    project_id: str
    coordinator_run: str
    ownership_generation: int
    writer: str
    written_at: str
    body_digest_field: str  # the 'body_digest' field value from the record


@dataclass(frozen=True)
class AttemptEnvelope:
    task_id: str
    attempt_id: str


# ---------------------------------------------------------------------------
# Record kind dataclasses (§7.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationRecord:
    plan_hash: str
    claims: Tuple[str, ...]
    counter: int


@dataclass(frozen=True)
class PreparedRecord:
    definition_hash: str
    plan_hash: str
    contract: Dict[str, Any]
    baseline: Dict[str, Any]
    instruction_digest: str
    deadline_at: str
    deadline_budget_s: int
    heartbeat_interval_s: int
    log_cap_bytes: int


@dataclass(frozen=True)
class ScopeRecord:
    scope_id: str
    kind: str
    check_id: Optional[str]
    sequence: Optional[int]
    claims: Tuple[str, ...]


@dataclass(frozen=True)
class LaunchRecord:
    start_outcome: str
    handle: Optional[str]
    capability_id: str
    started_at: str
    adapter: Dict[str, Any]


@dataclass(frozen=True)
class ResultRecord:
    outcome: str
    produced: Dict[str, Any]
    baseline_matched: bool
    captures: Tuple[Dict[str, Any], ...]
    summary: str
    refusal_code: Optional[str]


@dataclass(frozen=True)
class SealedRecord:
    scope_id: str
    exit: Dict[str, Any]
    tree_exited: bool
    resumable: bool


@dataclass(frozen=True)
class CaptureRecord:
    check_id: str
    sequence: int
    kind: str
    argv: Optional[Tuple[str, ...]]
    cwd: str
    started_at: str
    ended_at: str
    exit: Optional[Dict[str, Any]]
    stdout_ref: Optional[str]
    stderr_ref: Optional[str]
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    subjects: Tuple[Dict[str, Any], ...]
    criteria: Optional[str]


@dataclass(frozen=True)
class AssessmentRecord:
    check_id: str
    sequence: int
    capture_id: str
    capture_body_digest: str
    assessor: Dict[str, Any]
    adequate: bool
    rationale: str


@dataclass(frozen=True)
class ClassificationRecord:
    cls: str  # maps from 'class' in JSON (reserved word in Python)
    evidence: Tuple[Dict[str, Any], ...]
    conflict_digests: Optional[Tuple[str, ...]]


@dataclass(frozen=True)
class HoldRecord:
    cause_id: str
    cause_class: str
    detail: str
    raised_by: str


@dataclass(frozen=True)
class ResolutionRecord:
    cause_id: str
    resolution: str
    observed_revision: int
    decided_by: str
    rationale: str
    evidence: Optional[Tuple[Dict[str, Any], ...]]


@dataclass(frozen=True)
class StopEvidenceRecord:
    kind: str
    observed_at: str
    detail: str
    scopes: Tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceRecord:
    intent: str
    receipt_id_field: str  # maps from 'receipt_id' in JSON
    definition_hash: str
    accepted_snapshot: Dict[str, Any]
    qualifying_captures: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class FenceRecord:
    receipt_id_field: str  # maps from 'receipt_id' in JSON
    sequence: int
    expected_revision: int
    expected_run: str
    expected_generation: int
    submitted_at: str


@dataclass(frozen=True)
class CommitObservedRecord:
    receipt_id_field: str  # maps from 'receipt_id' in JSON
    fence_sequence: int
    committed_revision: int
    evidence_index: int
    committed_status: str


@dataclass(frozen=True)
class GenerationFenceRecord:
    generation: int
    taken_over_from: int
    scope: str
    records: Tuple[str, ...]


@dataclass(frozen=True)
class ReleaseRecord:
    receipt: Optional[str]
    sealed_scopes: Tuple[str, ...]
    grant_removed: bool
    reason: str


@dataclass(frozen=True)
class OwnerRecord:
    host_id: str
    boot_id: str
    pid: int
    process_start: str
    started_at: str
    generation: int


@dataclass(frozen=True)
class StopRequestRecord:
    sequence: int
    requested_by: str
    reason: str


@dataclass(frozen=True)
class StopClearanceRecord:
    sequence: int
    clears: int
    cleared_by: str
    rationale: str


@dataclass(frozen=True)
class HeartbeatRecord:
    phase: str
    observed_at: str


# ---------------------------------------------------------------------------
# Record kind metadata
# ---------------------------------------------------------------------------

_PROJECT_LEVEL_KINDS = frozenset({"owner", "stop-request", "stop-clearance"})

_KNOWN_RECORD_KINDS = frozenset(
    {
        "reservation",
        "prepared",
        "scope",
        "launch",
        "result",
        "sealed",
        "capture",
        "assessment",
        "classification",
        "hold",
        "resolution",
        "stop-evidence",
        "acceptance",
        "fence",
        "commit-observed",
        "generation-fence",
        "release",
        "owner",
        "stop-request",
        "stop-clearance",
        "heartbeat",
    }
)


# ---------------------------------------------------------------------------
# validate_record (§7.4)
# ---------------------------------------------------------------------------


def _req(raw: Dict[str, Any], key: str, expected_type: type, kind: str) -> Any:
    if key not in raw:
        raise ExecutionRecordError(f"{kind}: missing required field {key!r}")
    v = raw[key]
    if not isinstance(v, expected_type):
        raise ExecutionRecordError(
            f"{kind}: field {key!r} must be {expected_type.__name__}, got {type(v).__name__!r}"
        )
    return v


def _opt(raw: Dict[str, Any], key: str, expected_type: type, kind: str) -> Any:
    if key not in raw or raw[key] is None:
        return None
    v = raw[key]
    if not isinstance(v, expected_type):
        raise ExecutionRecordError(
            f"{kind}: field {key!r} must be {expected_type.__name__} or null, got {type(v).__name__!r}"
        )
    return v


def _build_kind_record(kind: str, raw: Dict[str, Any]) -> Any:
    if kind == "reservation":
        return ReservationRecord(
            plan_hash=_req(raw, "plan_hash", str, kind),
            claims=tuple(_req(raw, "claims", list, kind)),
            counter=_req(raw, "counter", int, kind),
        )
    if kind == "prepared":
        return PreparedRecord(
            definition_hash=_req(raw, "definition_hash", str, kind),
            plan_hash=_req(raw, "plan_hash", str, kind),
            contract=_req(raw, "contract", dict, kind),
            baseline=_req(raw, "baseline", dict, kind),
            instruction_digest=_req(raw, "instruction_digest", str, kind),
            deadline_at=_req(raw, "deadline_at", str, kind),
            deadline_budget_s=_req(raw, "deadline_budget_s", int, kind),
            heartbeat_interval_s=_req(raw, "heartbeat_interval_s", int, kind),
            log_cap_bytes=_req(raw, "log_cap_bytes", int, kind),
        )
    if kind == "scope":
        return ScopeRecord(
            scope_id=_req(raw, "scope_id", str, kind),
            kind=_req(raw, "kind", str, kind),
            check_id=_opt(raw, "check_id", str, kind),
            sequence=_opt(raw, "sequence", int, kind),
            claims=tuple(_req(raw, "claims", list, kind)),
        )
    if kind == "launch":
        return LaunchRecord(
            start_outcome=_req(raw, "start_outcome", str, kind),
            handle=_opt(raw, "handle", str, kind),
            capability_id=_req(raw, "capability_id", str, kind),
            started_at=_req(raw, "started_at", str, kind),
            adapter=_req(raw, "adapter", dict, kind),
        )
    if kind == "result":
        return ResultRecord(
            outcome=_req(raw, "outcome", str, kind),
            produced=_req(raw, "produced", dict, kind),
            baseline_matched=_req(raw, "baseline_matched", bool, kind),
            captures=tuple(_req(raw, "captures", list, kind)),
            summary=_req(raw, "summary", str, kind),
            refusal_code=_opt(raw, "refusal_code", str, kind),
        )
    if kind == "sealed":
        return SealedRecord(
            scope_id=_req(raw, "scope_id", str, kind),
            exit=_req(raw, "exit", dict, kind),
            tree_exited=_req(raw, "tree_exited", bool, kind),
            resumable=_req(raw, "resumable", bool, kind),
        )
    if kind == "capture":
        argv_raw = raw.get("argv")
        argv: Optional[Tuple[str, ...]] = tuple(argv_raw) if isinstance(argv_raw, list) else None
        return CaptureRecord(
            check_id=_req(raw, "check_id", str, kind),
            sequence=_req(raw, "sequence", int, kind),
            kind=_req(raw, "kind", str, kind),
            argv=argv,
            cwd=_req(raw, "cwd", str, kind),
            started_at=_req(raw, "started_at", str, kind),
            ended_at=_req(raw, "ended_at", str, kind),
            exit=_opt(raw, "exit", dict, kind),
            stdout_ref=_opt(raw, "stdout_ref", str, kind),
            stderr_ref=_opt(raw, "stderr_ref", str, kind),
            stdout_bytes=_req(raw, "stdout_bytes", int, kind),
            stderr_bytes=_req(raw, "stderr_bytes", int, kind),
            stdout_truncated=_req(raw, "stdout_truncated", bool, kind),
            stderr_truncated=_req(raw, "stderr_truncated", bool, kind),
            subjects=tuple(_req(raw, "subjects", list, kind)),
            criteria=_opt(raw, "criteria", str, kind),
        )
    if kind == "assessment":
        return AssessmentRecord(
            check_id=_req(raw, "check_id", str, kind),
            sequence=_req(raw, "sequence", int, kind),
            capture_id=_req(raw, "capture_id", str, kind),
            capture_body_digest=_req(raw, "capture_body_digest", str, kind),
            assessor=_req(raw, "assessor", dict, kind),
            adequate=_req(raw, "adequate", bool, kind),
            rationale=_req(raw, "rationale", str, kind),
        )
    if kind == "classification":
        if "class" not in raw:
            raise ExecutionRecordError(f"{kind}: missing required field 'class'")
        cls_val = raw["class"]
        if not isinstance(cls_val, str):
            raise ExecutionRecordError(f"{kind}: field 'class' must be str")
        cd_raw = raw.get("conflict_digests")
        conflict_digests: Optional[Tuple[str, ...]] = tuple(cd_raw) if isinstance(cd_raw, list) else None
        return ClassificationRecord(
            cls=cls_val,
            evidence=tuple(_req(raw, "evidence", list, kind)),
            conflict_digests=conflict_digests,
        )
    if kind == "hold":
        return HoldRecord(
            cause_id=_req(raw, "cause_id", str, kind),
            cause_class=_req(raw, "cause_class", str, kind),
            detail=_req(raw, "detail", str, kind),
            raised_by=_req(raw, "raised_by", str, kind),
        )
    if kind == "resolution":
        ev_raw = raw.get("evidence")
        evidence: Optional[Tuple[Dict[str, Any], ...]] = tuple(ev_raw) if isinstance(ev_raw, list) else None
        return ResolutionRecord(
            cause_id=_req(raw, "cause_id", str, kind),
            resolution=_req(raw, "resolution", str, kind),
            observed_revision=_req(raw, "observed_revision", int, kind),
            decided_by=_req(raw, "decided_by", str, kind),
            rationale=_req(raw, "rationale", str, kind),
            evidence=evidence,
        )
    if kind == "stop-evidence":
        return StopEvidenceRecord(
            kind=_req(raw, "kind", str, kind),
            observed_at=_req(raw, "observed_at", str, kind),
            detail=_req(raw, "detail", str, kind),
            scopes=tuple(_req(raw, "scopes", list, kind)),
        )
    if kind == "acceptance":
        return AcceptanceRecord(
            intent=_req(raw, "intent", str, kind),
            receipt_id_field=_req(raw, "receipt_id", str, kind),
            definition_hash=_req(raw, "definition_hash", str, kind),
            accepted_snapshot=_req(raw, "accepted_snapshot", dict, kind),
            qualifying_captures=tuple(_req(raw, "qualifying_captures", list, kind)),
        )
    if kind == "fence":
        return FenceRecord(
            receipt_id_field=_req(raw, "receipt_id", str, kind),
            sequence=_req(raw, "sequence", int, kind),
            expected_revision=_req(raw, "expected_revision", int, kind),
            expected_run=_req(raw, "expected_run", str, kind),
            expected_generation=_req(raw, "expected_generation", int, kind),
            submitted_at=_req(raw, "submitted_at", str, kind),
        )
    if kind == "commit-observed":
        return CommitObservedRecord(
            receipt_id_field=_req(raw, "receipt_id", str, kind),
            fence_sequence=_req(raw, "fence_sequence", int, kind),
            committed_revision=_req(raw, "committed_revision", int, kind),
            evidence_index=_req(raw, "evidence_index", int, kind),
            committed_status=_req(raw, "committed_status", str, kind),
        )
    if kind == "generation-fence":
        return GenerationFenceRecord(
            generation=_req(raw, "generation", int, kind),
            taken_over_from=_req(raw, "taken_over_from", int, kind),
            scope=_req(raw, "scope", str, kind),
            records=tuple(_req(raw, "records", list, kind)),
        )
    if kind == "release":
        receipt_val = raw.get("receipt")
        if "receipt" not in raw:
            raise ExecutionRecordError(f"{kind}: missing required field 'receipt'")
        if receipt_val is not None and not isinstance(receipt_val, str):
            raise ExecutionRecordError(f"{kind}: field 'receipt' must be str or null")
        return ReleaseRecord(
            receipt=receipt_val,
            sealed_scopes=tuple(_req(raw, "sealed_scopes", list, kind)),
            grant_removed=_req(raw, "grant_removed", bool, kind),
            reason=_req(raw, "reason", str, kind),
        )
    if kind == "owner":
        return OwnerRecord(
            host_id=_req(raw, "host_id", str, kind),
            boot_id=_req(raw, "boot_id", str, kind),
            pid=_req(raw, "pid", int, kind),
            process_start=_req(raw, "process_start", str, kind),
            started_at=_req(raw, "started_at", str, kind),
            generation=_req(raw, "generation", int, kind),
        )
    if kind == "stop-request":
        return StopRequestRecord(
            sequence=_req(raw, "sequence", int, kind),
            requested_by=_req(raw, "requested_by", str, kind),
            reason=_req(raw, "reason", str, kind),
        )
    if kind == "stop-clearance":
        return StopClearanceRecord(
            sequence=_req(raw, "sequence", int, kind),
            clears=_req(raw, "clears", int, kind),
            cleared_by=_req(raw, "cleared_by", str, kind),
            rationale=_req(raw, "rationale", str, kind),
        )
    # heartbeat
    return HeartbeatRecord(
        phase=_req(raw, "phase", str, kind),
        observed_at=_req(raw, "observed_at", str, kind),
    )


def validate_record(
    raw: Any,
) -> Tuple[CommonEnvelope, Optional[AttemptEnvelope], Any]:
    """Validate a raw value parsed from JSON and return (common_envelope, attempt_envelope, kind_record).

    Raises ExecutionRecordError on null, missing fields, wrong types, unknown kind, or bad version.
    Per §6.3: a stored null is a record that exists and cannot be read, not absence.
    """
    if raw is None:
        raise ExecutionRecordError("stored null: record exists but cannot be read (§6.3)")
    if not isinstance(raw, dict):
        raise ExecutionRecordError(f"record must be a JSON object, got {type(raw).__name__!r}")

    kind = _req(raw, "record_kind", str, "record")
    if kind not in _KNOWN_RECORD_KINDS:
        raise ExecutionRecordError(f"unknown record_kind: {kind!r}")

    version = _req(raw, "schema_version", str, kind)
    parts = version.split(".", 1)
    if not parts[0].isdigit() or int(parts[0]) != 1:
        raise ExecutionRecordError(f"{kind}: schema_version major must be 1, got {version!r}")

    project_id = _req(raw, "project_id", str, kind)
    coordinator_run = _req(raw, "coordinator_run", str, kind)
    ownership_generation = _req(raw, "ownership_generation", int, kind)
    writer = _req(raw, "writer", str, kind)
    if writer not in _VALID_WRITERS:
        raise ExecutionRecordError(f"{kind}: unknown writer {writer!r}")
    written_at = _req(raw, "written_at", str, kind)
    body_digest_val = _req(raw, "body_digest", str, kind)

    common = CommonEnvelope(
        record_kind=kind,
        schema_version=version,
        project_id=project_id,
        coordinator_run=coordinator_run,
        ownership_generation=ownership_generation,
        writer=writer,
        written_at=written_at,
        body_digest_field=body_digest_val,
    )

    # Determine attempt-scope: project-level kinds never have an attempt envelope.
    # generation-fence is determined by its own 'scope' field.
    attempt_envelope: Optional[AttemptEnvelope] = None
    if kind not in _PROJECT_LEVEL_KINDS:
        if kind == "generation-fence":
            scope_field = raw.get("scope")
            if scope_field != "project":
                task_id = _req(raw, "task_id", str, kind)
                attempt_id = _req(raw, "attempt_id", str, kind)
                attempt_envelope = AttemptEnvelope(task_id=task_id, attempt_id=attempt_id)
        else:
            task_id = _req(raw, "task_id", str, kind)
            attempt_id = _req(raw, "attempt_id", str, kind)
            attempt_envelope = AttemptEnvelope(task_id=task_id, attempt_id=attempt_id)

    kind_record = _build_kind_record(kind, raw)
    return common, attempt_envelope, kind_record
