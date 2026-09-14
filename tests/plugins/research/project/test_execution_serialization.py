"""Tests for execution_serialization: canonical_json, digests, ids, record schemas.

Coverage must be 100% over execution_serialization; every test establishes a named
property rather than merely describing a call.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from execution_serialization import (
    CAUSE_CLASSES,
    CLASSIFICATION_CLASSES,
    AcceptanceRecord,
    AssessmentRecord,
    AttemptEnvelope,
    CaptureRecord,
    ClassificationRecord,
    CommitObservedRecord,
    CommonEnvelope,
    ExecutionIdError,
    ExecutionRecordError,
    FenceRecord,
    GenerationFenceRecord,
    HeartbeatRecord,
    HoldRecord,
    LaunchRecord,
    OwnerRecord,
    PreparedRecord,
    ReleaseRecord,
    ReservationRecord,
    ResolutionRecord,
    ResultRecord,
    ScopeRecord,
    SealedRecord,
    StopClearanceRecord,
    StopEvidenceRecord,
    StopRequestRecord,
    body_digest,
    canonical_json,
    capture_id,
    content_digest,
    decode_scope_id,
    encode_scope_id,
    format_claim_string,
    parse_claim_string,
    receipt_id,
    validate_cause_class,
    validate_id,
    validate_record,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base(kind: str, **extra: object) -> dict:
    """Minimal common + attempt envelope dict for a non-project-level record kind."""
    d: dict = {
        "record_kind": kind,
        "schema_version": "1.0",
        "project_id": "proj-1",
        "coordinator_run": "run-1",
        "ownership_generation": 1,
        "writer": "coordinator",
        "written_at": "2026-01-01T00:00:00Z",
        "body_digest": "abc123",
        "task_id": "task-1",
        "attempt_id": "att-1",
    }
    d.update(extra)
    return d


def _project_base(kind: str, **extra: object) -> dict:
    """Common envelope only; no attempt envelope fields."""
    d: dict = {
        "record_kind": kind,
        "schema_version": "1.0",
        "project_id": "proj-1",
        "coordinator_run": "run-1",
        "ownership_generation": 1,
        "writer": "coordinator",
        "written_at": "2026-01-01T00:00:00Z",
        "body_digest": "abc123",
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# canonical_json
# ---------------------------------------------------------------------------


def test_canonical_json_returns_utf8_bytes() -> None:
    result = canonical_json({"b": 2, "a": 1})
    assert isinstance(result, bytes)
    assert json.loads(result.decode("utf-8")) == {"a": 1, "b": 2}


def test_canonical_json_sorts_keys_no_whitespace() -> None:
    assert canonical_json({"z": 0, "a": 1}) == b'{"a":1,"z":0}\n'


def test_canonical_json_trailing_newline() -> None:
    assert canonical_json({}).endswith(b"\n")


def test_canonical_json_nested_sorts_recursively() -> None:
    assert canonical_json({"x": {"c": 3, "b": 2}}) == b'{"x":{"b":2,"c":3}}\n'


# ---------------------------------------------------------------------------
# body_digest
# ---------------------------------------------------------------------------


def test_body_digest_is_64_hex_chars() -> None:
    d = body_digest({"k": "v"})
    assert len(d) == 64
    assert all(c in "0123456789abcdef" for c in d)


def test_body_digest_strips_body_digest_and_receipt_id_keys() -> None:
    rec = {"k": "v", "body_digest": "old-bd", "receipt_id": "old-rid"}
    expected = hashlib.sha256(canonical_json({"k": "v"})).hexdigest()
    assert body_digest(rec) == expected


def test_body_digest_without_stripped_keys_same_as_plain() -> None:
    rec = {"k": "v"}
    assert body_digest(rec) == hashlib.sha256(canonical_json({"k": "v"})).hexdigest()


# ---------------------------------------------------------------------------
# content_digest
# ---------------------------------------------------------------------------


def test_content_digest_strips_all_six_writer_stamped_fields() -> None:
    rec = {
        "k": "v",
        "body_digest": "x",
        "receipt_id": "y",
        "coordinator_run": "r",
        "ownership_generation": 1,
        "writer": "coordinator",
        "written_at": "2026",
    }
    assert content_digest(rec) == hashlib.sha256(canonical_json({"k": "v"})).hexdigest()


def test_content_digest_differs_from_body_digest_when_writer_stamped_present() -> None:
    rec = {"k": "v", "writer": "coordinator", "written_at": "t", "coordinator_run": "r", "ownership_generation": 1}
    assert content_digest(rec) != body_digest(rec)


# ---------------------------------------------------------------------------
# capture_id
# ---------------------------------------------------------------------------


def test_capture_id_starts_with_cap_prefix() -> None:
    cid = capture_id({"check_id": "c1", "sequence": 0})
    assert cid.startswith("cap-")


def test_capture_id_is_cap_plus_32_chars() -> None:
    cid = capture_id({"check_id": "c1", "sequence": 0})
    assert len(cid) == 36  # "cap-" (4) + 32 hex chars


def test_capture_id_is_deterministic() -> None:
    rec = {"check_id": "c1", "sequence": 0}
    assert capture_id(rec) == capture_id(rec)


# ---------------------------------------------------------------------------
# receipt_id
# ---------------------------------------------------------------------------


def test_receipt_id_starts_with_rcp_prefix() -> None:
    rid = receipt_id("att-1", "dh1", "run", {}, [])
    assert rid.startswith("rcp-")


def test_receipt_id_is_rcp_plus_32_chars() -> None:
    rid = receipt_id("att-1", "dh1", "run", {}, [])
    assert len(rid) == 36  # "rcp-" (4) + 32 hex chars


def test_receipt_id_is_deterministic() -> None:
    assert receipt_id("att-1", "dh1", "run", {}, []) == receipt_id("att-1", "dh1", "run", {}, [])


def test_receipt_id_varies_with_attempt_id() -> None:
    assert receipt_id("att-1", "dh1", "run", {}, []) != receipt_id("att-2", "dh1", "run", {}, [])


# ---------------------------------------------------------------------------
# encode_scope_id / decode_scope_id
# ---------------------------------------------------------------------------


def test_encode_scope_id_replaces_colon_and_hash() -> None:
    assert encode_scope_id("ns:key#frag") == "ns%3Akey%23frag"


def test_encode_scope_id_no_op_when_no_special_chars() -> None:
    assert encode_scope_id("simple-id_1.2~3") == "simple-id_1.2~3"


def test_decode_scope_id_reverses_encoding() -> None:
    assert decode_scope_id("ns%3Akey%23frag") == "ns:key#frag"


def test_encode_decode_roundtrip() -> None:
    original = "ns:key/sub#frag"
    assert decode_scope_id(encode_scope_id(original)) == original


# ---------------------------------------------------------------------------
# validate_id
# ---------------------------------------------------------------------------


def test_validate_id_accepts_alphanum_and_safe_punctuation() -> None:
    for value in ("abc", "ABC", "123", "a-b", "a_b", "a.b", "a~b", "aA1-_.~"):
        assert validate_id(value, "f") == value


def test_validate_id_rejects_non_string() -> None:
    with pytest.raises(ExecutionIdError, match="must be str"):
        validate_id(123, "field")


def test_validate_id_rejects_empty_string() -> None:
    with pytest.raises(ExecutionIdError, match="must not be empty"):
        validate_id("", "field")


def test_validate_id_rejects_slash() -> None:
    with pytest.raises(ExecutionIdError, match="invalid in a path segment"):
        validate_id("a/b", "field")


def test_validate_id_rejects_colon() -> None:
    with pytest.raises(ExecutionIdError, match="invalid in a path segment"):
        validate_id("a:b", "field")


def test_validate_id_rejects_whitespace() -> None:
    with pytest.raises(ExecutionIdError, match="invalid in a path segment"):
        validate_id("a b", "field")


# ---------------------------------------------------------------------------
# CAUSE_CLASSES / CLASSIFICATION_CLASSES / validate_cause_class
# ---------------------------------------------------------------------------


def test_cause_classes_contains_14_entries() -> None:
    assert len(CAUSE_CLASSES) == 14


def test_cause_classes_contains_all_expected() -> None:
    expected = {
        "check_failure", "indeterminate_check", "flaky_check", "plan_defect",
        "stale_evidence", "definition_changed", "conflicting_publication",
        "corrupt_record", "store_error", "authorization_lost", "lost_contact",
        "human_review", "operator_stop", "unsafe_release",
    }
    assert CAUSE_CLASSES == expected


def test_classification_classes_contains_8_entries() -> None:
    assert len(CLASSIFICATION_CLASSES) == 8


def test_validate_cause_class_accepts_every_cause() -> None:
    for cls in CAUSE_CLASSES:
        assert validate_cause_class(cls) == cls


def test_validate_cause_class_rejects_unknown_string() -> None:
    with pytest.raises(ExecutionIdError, match="unknown cause class"):
        validate_cause_class("nonexistent")


def test_validate_cause_class_rejects_non_string() -> None:
    with pytest.raises(ExecutionIdError, match="unknown cause class"):
        validate_cause_class(None)


# ---------------------------------------------------------------------------
# parse_claim_string
# ---------------------------------------------------------------------------


def test_parse_claim_string_local_read() -> None:
    assert parse_claim_string("local:my-key read") == ("local", "my-key", "read")


def test_parse_claim_string_store_write() -> None:
    assert parse_claim_string("store:att-123 write") == ("store", "att-123", "write")


def test_parse_claim_string_key_with_embedded_colon() -> None:
    ns, key, acc = parse_claim_string("local:a:b read")
    assert (ns, key, acc) == ("local", "a:b", "read")


def test_parse_claim_string_rejects_non_string() -> None:
    with pytest.raises(ExecutionIdError, match="must be str"):
        parse_claim_string(42)


def test_parse_claim_string_rejects_missing_space() -> None:
    with pytest.raises(ExecutionIdError, match="missing space separator"):
        parse_claim_string("local:key")


def test_parse_claim_string_rejects_bad_access() -> None:
    with pytest.raises(ExecutionIdError, match="unknown access"):
        parse_claim_string("local:key delete")


def test_parse_claim_string_rejects_missing_namespace_separator() -> None:
    with pytest.raises(ExecutionIdError, match="missing namespace separator"):
        parse_claim_string("localkey read")


def test_parse_claim_string_rejects_unknown_namespace() -> None:
    with pytest.raises(ExecutionIdError, match="unknown namespace"):
        parse_claim_string("external:key read")


def test_parse_claim_string_rejects_empty_key() -> None:
    with pytest.raises(ExecutionIdError, match="key must not be empty"):
        parse_claim_string("local: read")


# ---------------------------------------------------------------------------
# format_claim_string
# ---------------------------------------------------------------------------


def test_format_claim_string_produces_expected_string() -> None:
    assert format_claim_string("local", "my-key", "write") == "local:my-key write"


def test_format_claim_string_rejects_unknown_namespace() -> None:
    with pytest.raises(ExecutionIdError, match="unknown namespace"):
        format_claim_string("external", "k", "read")


def test_format_claim_string_rejects_unknown_access() -> None:
    with pytest.raises(ExecutionIdError, match="unknown access"):
        format_claim_string("local", "k", "delete")


def test_format_claim_string_rejects_empty_key() -> None:
    with pytest.raises(ExecutionIdError, match="key must not be empty"):
        format_claim_string("local", "", "read")


# ---------------------------------------------------------------------------
# validate_record — common envelope and attempt envelope extraction
# ---------------------------------------------------------------------------


def test_validate_record_common_envelope_fields_populated() -> None:
    raw = _base("heartbeat", phase="running", observed_at="2026-01-01T00:00:00Z")
    common, _attempt, _ = validate_record(raw)
    assert isinstance(common, CommonEnvelope)
    assert common.record_kind == "heartbeat"
    assert common.schema_version == "1.0"
    assert common.project_id == "proj-1"
    assert common.coordinator_run == "run-1"
    assert common.ownership_generation == 1
    assert common.writer == "coordinator"
    assert common.written_at == "2026-01-01T00:00:00Z"
    assert common.body_digest_field == "abc123"


def test_validate_record_attempt_envelope_populated_for_non_project_kinds() -> None:
    raw = _base("heartbeat", phase="running", observed_at="2026-01-01T00:00:00Z")
    _, attempt, _ = validate_record(raw)
    assert isinstance(attempt, AttemptEnvelope)
    assert attempt.task_id == "task-1"
    assert attempt.attempt_id == "att-1"


# ---------------------------------------------------------------------------
# validate_record — all 21 record kinds (success paths)
# ---------------------------------------------------------------------------


def test_validate_record_reservation() -> None:
    raw = _base("reservation", plan_hash="ph1", claims=["local:a write"], counter=0)
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, ReservationRecord)
    assert rec.plan_hash == "ph1"
    assert rec.claims == ("local:a write",)
    assert rec.counter == 0
    assert isinstance(attempt, AttemptEnvelope)


def test_validate_record_prepared() -> None:
    raw = _base(
        "prepared",
        definition_hash="dh1",
        plan_hash="ph1",
        contract={"k": "v"},
        baseline={"b": 1},
        instruction_digest="id1",
        deadline_at="2026-01-02T00:00:00Z",
        deadline_budget_s=3600,
        heartbeat_interval_s=60,
        log_cap_bytes=1024,
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, PreparedRecord)
    assert rec.definition_hash == "dh1"
    assert rec.deadline_budget_s == 3600


def test_validate_record_scope_with_optionals_present() -> None:
    raw = _base("scope", scope_id="s1", check_id="c1", sequence=0, claims=[])
    raw["kind"] = "check"
    _, _, rec = validate_record(raw)
    assert isinstance(rec, ScopeRecord)
    assert rec.check_id == "c1"
    assert rec.sequence == 0


def test_validate_record_scope_optionals_absent() -> None:
    # Keys entirely absent → _opt returns None via `key not in raw` branch
    raw = _base("scope", scope_id="s1", claims=[])
    raw["kind"] = "run"
    _, _, rec = validate_record(raw)
    assert rec.check_id is None
    assert rec.sequence is None


def test_validate_record_scope_optionals_null() -> None:
    # Keys present but null → _opt returns None via `raw[key] is None` branch
    raw = _base("scope", scope_id="s1", check_id=None, sequence=None, claims=[])
    raw["kind"] = "run"
    _, _, rec = validate_record(raw)
    assert rec.check_id is None
    assert rec.sequence is None


def test_validate_record_launch_with_handle() -> None:
    raw = _base(
        "launch",
        start_outcome="started",
        handle="h1",
        capability_id="cap1",
        started_at="2026-01-01T00:00:00Z",
        adapter={"type": "local"},
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, LaunchRecord)
    assert rec.handle == "h1"


def test_validate_record_launch_no_handle() -> None:
    raw = _base("launch", start_outcome="failed", capability_id="cap1", started_at="t", adapter={})
    _, _, rec = validate_record(raw)
    assert rec.handle is None


def test_validate_record_result_with_refusal_code() -> None:
    raw = _base(
        "result",
        outcome="refused",
        produced={},
        baseline_matched=False,
        captures=[],
        summary="refused",
        refusal_code="R001",
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, ResultRecord)
    assert rec.refusal_code == "R001"


def test_validate_record_result_no_refusal_code() -> None:
    raw = _base("result", outcome="success", produced={}, baseline_matched=True, captures=[], summary="ok")
    _, _, rec = validate_record(raw)
    assert rec.refusal_code is None


def test_validate_record_sealed() -> None:
    raw = _base("sealed", scope_id="s1", exit={"code": 0}, tree_exited=True, resumable=False)
    _, _, rec = validate_record(raw)
    assert isinstance(rec, SealedRecord)
    assert rec.tree_exited is True


def test_validate_record_capture_with_argv_and_exit() -> None:
    raw = _base(
        "capture",
        check_id="c1",
        sequence=0,
        argv=["pytest", "-q"],
        cwd="/repo",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:01:00Z",
        exit={"code": 0},
        stdout_ref=None,
        stderr_ref=None,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        subjects=[],
        criteria=None,
    )
    raw["kind"] = "shell"
    _, _, rec = validate_record(raw)
    assert isinstance(rec, CaptureRecord)
    assert rec.argv == ("pytest", "-q")
    assert rec.exit == {"code": 0}


def test_validate_record_capture_no_argv_no_optional_fields() -> None:
    # argv absent → None; exit absent → None; stdout_ref absent → None; criteria absent → None
    raw = _base(
        "capture",
        check_id="c1",
        sequence=1,
        cwd="/repo",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:01:00Z",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        subjects=[],
    )
    raw["kind"] = "shell"
    _, _, rec = validate_record(raw)
    assert rec.argv is None
    assert rec.exit is None
    assert rec.stdout_ref is None
    assert rec.stderr_ref is None
    assert rec.criteria is None


def test_validate_record_assessment() -> None:
    raw = _base(
        "assessment",
        check_id="c1",
        sequence=0,
        capture_id="cap-abc",
        capture_body_digest="d" * 64,
        assessor={"kind": "auto"},
        adequate=True,
        rationale="looks good",
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, AssessmentRecord)
    assert rec.adequate is True


def test_validate_record_classification_with_conflict_digests() -> None:
    raw = _base("classification", **{"class": "success"}, evidence=[], conflict_digests=["d1", "d2"])
    _, _, rec = validate_record(raw)
    assert isinstance(rec, ClassificationRecord)
    assert rec.cls == "success"
    assert rec.conflict_digests == ("d1", "d2")


def test_validate_record_classification_no_conflict_digests() -> None:
    raw = _base("classification", **{"class": "stopped"}, evidence=[])
    _, _, rec = validate_record(raw)
    assert rec.conflict_digests is None


def test_validate_record_hold() -> None:
    raw = _base("hold", cause_id="cause-1", cause_class="check_failure", detail="failed", raised_by="coordinator")
    _, _, rec = validate_record(raw)
    assert isinstance(rec, HoldRecord)
    assert rec.cause_class == "check_failure"


def test_validate_record_resolution_with_evidence() -> None:
    raw = _base(
        "resolution",
        cause_id="cause-1",
        resolution="resolved",
        observed_revision=5,
        decided_by="operator",
        rationale="fixed",
        evidence=[{"k": "v"}],
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, ResolutionRecord)
    assert rec.evidence == ({"k": "v"},)


def test_validate_record_resolution_no_evidence() -> None:
    raw = _base(
        "resolution", cause_id="cause-1", resolution="dismissed",
        observed_revision=3, decided_by="auto", rationale="n/a",
    )
    _, _, rec = validate_record(raw)
    assert rec.evidence is None


def test_validate_record_stop_evidence() -> None:
    raw = _base("stop-evidence", observed_at="2026-01-01T00:00:00Z", detail="past deadline", scopes=["s1"])
    raw["kind"] = "deadline"
    _, _, rec = validate_record(raw)
    assert isinstance(rec, StopEvidenceRecord)
    assert rec.scopes == ("s1",)


def test_validate_record_acceptance() -> None:
    raw = _base(
        "acceptance",
        intent="publish",
        receipt_id="rcp-" + "a" * 32,
        definition_hash="dh1",
        accepted_snapshot={"rev": 1},
        qualifying_captures=[],
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, AcceptanceRecord)
    assert rec.intent == "publish"
    assert rec.receipt_id_field == "rcp-" + "a" * 32


def test_validate_record_fence() -> None:
    raw = _base(
        "fence",
        receipt_id="rcp-" + "b" * 32,
        sequence=1,
        expected_revision=5,
        expected_run="run-1",
        expected_generation=2,
        submitted_at="2026-01-01T00:00:00Z",
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, FenceRecord)
    assert rec.sequence == 1
    assert rec.receipt_id_field == "rcp-" + "b" * 32


def test_validate_record_commit_observed() -> None:
    raw = _base(
        "commit-observed",
        receipt_id="rcp-" + "c" * 32,
        fence_sequence=1,
        committed_revision=6,
        evidence_index=0,
        committed_status="committed",
    )
    _, _, rec = validate_record(raw)
    assert isinstance(rec, CommitObservedRecord)
    assert rec.committed_revision == 6
    assert rec.receipt_id_field == "rcp-" + "c" * 32


def test_validate_record_generation_fence_project_scope_no_attempt_envelope() -> None:
    raw = _project_base("generation-fence", generation=3, taken_over_from=2, scope="project", records=["r1"])
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, GenerationFenceRecord)
    assert rec.scope == "project"
    assert attempt is None


def test_validate_record_generation_fence_attempt_scope_has_attempt_envelope() -> None:
    raw = _base("generation-fence", generation=3, taken_over_from=2, scope="attempt", records=[])
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, GenerationFenceRecord)
    assert rec.scope == "attempt"
    assert isinstance(attempt, AttemptEnvelope)


def test_validate_record_release_with_string_receipt() -> None:
    raw = _base("release", receipt="rcp-" + "d" * 32, sealed_scopes=["s1"], grant_removed=True, reason="completed")
    _, _, rec = validate_record(raw)
    assert isinstance(rec, ReleaseRecord)
    assert rec.receipt == "rcp-" + "d" * 32
    assert rec.grant_removed is True


def test_validate_record_release_null_receipt() -> None:
    raw = _base("release", receipt=None, sealed_scopes=[], grant_removed=False, reason="aborted")
    _, _, rec = validate_record(raw)
    assert rec.receipt is None


def test_validate_record_owner_no_attempt_envelope() -> None:
    raw = _project_base(
        "owner",
        host_id="host-1",
        boot_id="boot-1",
        pid=12345,
        process_start="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:01Z",
        generation=1,
    )
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, OwnerRecord)
    assert attempt is None


def test_validate_record_stop_request_no_attempt_envelope() -> None:
    raw = _project_base("stop-request", sequence=1, requested_by="operator", reason="user request")
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, StopRequestRecord)
    assert attempt is None


def test_validate_record_stop_clearance_no_attempt_envelope() -> None:
    raw = _project_base("stop-clearance", sequence=1, clears=1, cleared_by="coordinator", rationale="gone")
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, StopClearanceRecord)
    assert attempt is None


def test_validate_record_heartbeat() -> None:
    raw = _base("heartbeat", phase="checking", observed_at="2026-01-01T00:00:00Z")
    _, attempt, rec = validate_record(raw)
    assert isinstance(rec, HeartbeatRecord)
    assert rec.phase == "checking"
    assert isinstance(attempt, AttemptEnvelope)


# ---------------------------------------------------------------------------
# validate_record — error paths
# ---------------------------------------------------------------------------


def test_validate_record_none_raises_stored_null_error() -> None:
    with pytest.raises(ExecutionRecordError, match="stored null"):
        validate_record(None)


def test_validate_record_non_dict_raises() -> None:
    with pytest.raises(ExecutionRecordError, match="JSON object"):
        validate_record([1, 2, 3])


def test_validate_record_unknown_kind_raises() -> None:
    raw = _base("not-a-kind", phase="x", observed_at="t")
    with pytest.raises(ExecutionRecordError, match="unknown record_kind"):
        validate_record(raw)


def test_validate_record_schema_version_major_not_1_raises() -> None:
    raw = _base("heartbeat", phase="x", observed_at="t")
    raw["schema_version"] = "2.0"
    with pytest.raises(ExecutionRecordError, match="schema_version major must be 1"):
        validate_record(raw)


def test_validate_record_schema_version_non_digit_major_raises() -> None:
    raw = _base("heartbeat", phase="x", observed_at="t")
    raw["schema_version"] = "abc.0"
    with pytest.raises(ExecutionRecordError, match="schema_version major must be 1"):
        validate_record(raw)


def test_validate_record_unknown_writer_raises() -> None:
    raw = _base("heartbeat", phase="x", observed_at="t")
    raw["writer"] = "hacker"
    with pytest.raises(ExecutionRecordError, match="unknown writer"):
        validate_record(raw)


def test_validate_record_missing_required_field_raises() -> None:
    # reservation missing the required `counter` field
    raw = _base("reservation", plan_hash="ph1", claims=[])
    with pytest.raises(ExecutionRecordError, match="missing required field"):
        validate_record(raw)


def test_validate_record_wrong_type_for_required_field_raises() -> None:
    # reservation with `counter` as str instead of int
    raw = _base("reservation", plan_hash="ph1", claims=[], counter="not-an-int")
    with pytest.raises(ExecutionRecordError, match="must be int"):
        validate_record(raw)


def test_validate_record_optional_field_wrong_type_raises() -> None:
    # scope with check_id as int instead of str → _opt raises
    raw = _base("scope", scope_id="s1", check_id=123, claims=[])
    raw["kind"] = "check"
    with pytest.raises(ExecutionRecordError, match="must be str or null"):
        validate_record(raw)


def test_validate_record_classification_missing_class_field_raises() -> None:
    raw = _base("classification", evidence=[])
    with pytest.raises(ExecutionRecordError, match="missing required field 'class'"):
        validate_record(raw)


def test_validate_record_classification_class_wrong_type_raises() -> None:
    raw = _base("classification", evidence=[])
    raw["class"] = 42
    with pytest.raises(ExecutionRecordError, match="'class' must be str"):
        validate_record(raw)


def test_validate_record_release_missing_receipt_raises() -> None:
    raw = _base("release", sealed_scopes=[], grant_removed=False, reason="done")
    with pytest.raises(ExecutionRecordError, match="missing required field 'receipt'"):
        validate_record(raw)


def test_validate_record_release_non_string_non_null_receipt_raises() -> None:
    raw = _base("release", receipt=123, sealed_scopes=[], grant_removed=False, reason="done")
    with pytest.raises(ExecutionRecordError, match="'receipt' must be str or null"):
        validate_record(raw)
