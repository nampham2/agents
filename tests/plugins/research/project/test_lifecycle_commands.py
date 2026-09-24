"""End-to-end and failure-path contracts for named lifecycle compositions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import workspace_context as context
import workspace_journal as journal
import workspace_lib as lib
from workspace_operations import task_operation
from workspace_session import _load_state, update_project_data
from workspace_workflows import workflow

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_workflow_automation import fill_spec, invoke, task


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(
        tmp_path / "workspace", title="Lifecycle automation", working_directory=target, create_root=True
    )


def request(project: Path, identity: str = "operation", **fields: Any) -> dict[str, Any]:
    tokens = {path.relative_to(project).as_posix()[:-3]: journal.snapshot(path)[1] for path in project.rglob("*.md")}
    return {"id": identity, "expected_revision": _load_state(project)["revision"], "tokens": tokens, **fields}


def planned(project: Path) -> None:
    fill_spec(project)
    update_project_data(
        project, {"status": "PLANNING", "tasks": [task("T01"), task("T02", depends_on=["T01"])]}, expected_revision=0
    )


def done(project: Path) -> None:
    planned(project)
    task_operation(project, "start", "T01", expected_revision=1)
    result = workflow(
        project,
        "verify",
        {"id": "check", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('verified')"]}]},
    )
    record = result["attempts"][0]["result"]["record_id"]
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[record])
    task_operation(project, "skip", "T02", expected_revision=3, reason="Superseded within scope")


def test_round_confirmation_checkpoint_and_bundle(project: Path) -> None:
    sections = {heading: "Agreed content for " + heading for heading, _ in lib.SPEC_CANONICAL_SECTIONS}
    payload = request(
        project,
        sections=sections,
        decisions=[{"id": "decision-one", "body": "Actual user answer"}],
        architecture="# A1\n\n**Status:** draft\n\n## Modules\n\nOne parser.\n",
        continuation={"next": "Request confirmation", "questions": "Approve A1?"},
    )
    result = workflow(project, "round", payload)
    assert workflow(project, "round", payload) == result
    assert result["committed"] is False
    assert set(result["tokens"]) == {"spec", "architecture", "handoff"}
    assert result["tokens"]["handoff"] in result["resume_prompt"]
    assert "Next: Request confirmation" in result["resume_prompt"]
    assert workflow(project, "read", {"document": "spec", "entry": "decision-one"})["text"].endswith(
        "Actual user answer\n"
    )
    assert "One parser" in workflow(project, "read", {"document": "architecture", "section": "Modules"})["text"]
    for kind, name in (("requirements", "spec"), ("architecture", "architecture")):
        workflow(
            project,
            "confirm",
            request(
                project,
                "confirm-" + kind,
                kind=kind,
                proposal_sha256=journal.snapshot(project / (name + ".md"))[1],
                review_id="A1",
                response="I confirm this proposal",
                source="Current user message",
                scope=kind,
                continuation={"next": "Plan tasks", "questions": ""},
            ),
        )
    assert lib.architecture_status((project / "architecture.md").read_text()) == "agreed"
    assert "architecture.md" in (project / "spec.md").read_text()
    bundle = workflow(
        project, "resume", {"max_chars": 50, "selections": [{"document": "handoff"}, {"document": "spec"}]}
    )
    assert sum(len(source.get("text", "")) for source in bundle["sources"]) <= 100
    assert len(bundle["omitted"]) == 1
    assert bundle["memory_health"]["status"] == "no_matches"
    assert not bundle["freshness"]["revision_changed"]
    token = result["tokens"]["architecture"]
    assert "unchanged" not in workflow(project, "read", {"document": "architecture", "if_sha256": token})
    token = journal.snapshot(project / "architecture.md")[1]
    assert workflow(project, "read", {"document": "architecture", "if_sha256": token})["unchanged"]
    before = (project / "handoff.md").read_bytes()
    workflow(project, "checkpoint", request(project, "noop", continuation={}))
    assert (project / "handoff.md").read_bytes() == before


def test_corrections_reconciliation_reviews_and_finalization(project: Path) -> None:
    done(project)
    correction = task("ignored")
    correction.pop("id")
    created = workflow(
        project, "correct", request(project, replacement=correction, task="T01", reason="Follow-up defect")
    )
    assert created["task"] == "T03"
    assert created["assignment"]["evidence"] == []
    state = _load_state(project)
    assert state["tasks"][-1]["evidence"] == []
    assert state["tasks"][-1]["authorization"]["status"] == "not_required"
    impact = workflow(project, "impact", {"tasks": ["T01"], "paths": [{"root": "target", "path": "src"}]})
    assert [item["id"] for item in impact["candidates"]] == ["T01", "T02"]
    preview = workflow(
        project,
        "preview",
        {"expected_revision": state["revision"], "patch": {"tasks": [{"id": "T03", "depends_on": ["T01"]}]}},
    )
    assert preview["valid"] and preview["tasks"][0]["changes"]["depends_on"]["after"] == ["T01"]
    assert _load_state(project) == state
    workflow(
        project,
        "reconcile",
        request(
            project,
            "reconcile",
            patch={"tasks": [{"id": "T03", "status": "SKIPPED", "skip_reason": "Not needed after investigation"}]},
            decision="No additional implementation needed",
            continuation={"next": "Review delivery"},
        ),
    )
    for status in ("accepted", "pending", "accepted"):
        workflow(
            project,
            "review",
            request(
                project,
                "review-" + str(_load_state(project)["revision"]),
                reviewer="User",
                version="commit:abc",
                scope="Result",
                findings="Reviewed result",
                status=status,
                required=True,
            ),
        )
    assert len(list((project / "reviews").glob("*.md"))) == 3
    assert not workflow(project, "readiness", {})["valid"]
    final = workflow(
        project,
        "finalize",
        request(
            project,
            "final",
            reflection="# Reflection\n\nVerified parser. No open work.",
            continuation={"next": "No further work", "session": "ready for handoff"},
        ),
    )
    assert final["committed"] and _load_state(project)["status"] == "DONE"
    checkpoint = context.checkpoint_data(project)
    assert checkpoint is not None and checkpoint["revision"] == final["revision"]
    assert workflow(project, "readiness", {})["valid"]
    maintained = workflow(
        project,
        "maintenance",
        request(
            project,
            "maintenance",
            tasks=[task("T04")],
            reason="Requested follow-up",
            continuation={"next": "Start T04"},
        ),
    )
    assert "EXECUTING" in maintained["legal_next"]
    cancelled = workflow(
        project,
        "cancel",
        request(
            project,
            "cancel",
            reason="User cancelled follow-up",
            tasks=[{"id": "T04", "status": "SKIPPED", "skip_reason": "Cancelled"}],
            continuation={"next": "None"},
        ),
    )
    assert cancelled["legal_next"] == ["CANCELLED"]


def test_worker_packet_authorization_receipt_and_fingerprints(project: Path) -> None:
    planned(project)
    update_project_data(
        project,
        {
            "tasks": [
                {
                    "id": "T01",
                    "reads": ["src/input.py"],
                    "effect": {"kind": "external", "description": "Requested publication"},
                    "authorization": {"required": True, "status": "pending"},
                }
            ]
        },
        expected_revision=1,
    )
    workflow(
        project,
        "authorize",
        request(
            project,
            authorization={
                "required": True,
                "status": "explicit",
                "scope": "Publish version",
                "source": "Current user message",
                "authorized_at": lib.now_iso(),
            },
            task="T01",
        ),
    )
    receipt = {
        "kind": "publish",
        "value": "publish:actual-result",
        "destination": "Requested destination",
        "timestamp": lib.now_iso(),
    }
    workflow(project, "receipt", request(project, "receipt", task="T01", receipt=receipt))
    workflow(project, "receipt", request(project, "receipt-again", task="T01", receipt=receipt))
    assert len(_load_state(project)["tasks"][0]["receipts"]) == 1
    for event in ("intent", "launched"):
        workflow(
            project,
            "worker-event",
            request(
                project,
                event,
                task="T01",
                handle="native-123",
                event=event,
                scope="Review only",
                observation="Observed through host",
                assignment_revision=2,
            ),
        )
    packet = workflow(project, "packet", {"task": "T01", "worker": "reviewer", "scope": "Read only", "lessons": []})
    assert packet["unresolved_workers"][0]["handle"] == "native-123"
    assert packet["assignment"]["selected_task"]["id"] == "T01"
    workflow(
        project,
        "worker-event",
        request(
            project,
            "stopped",
            task="T01",
            handle="native-123",
            event="stopped",
            scope="Review only",
            observation="Host reports stopped",
            assignment_revision=2,
        ),
    )
    assert not workflow(project, "resume", {})["unresolved_workers"]
    impact = workflow(project, "impact", {"tasks": [], "paths": [{"root": "target", "path": "src"}]})
    assert impact["overlaps"][0]["task"] == "T01"
    refs = [{"root": "workspace", "path": "spec.md"}, {"root": "target", "path": "absent", "sha256": "missing"}]
    fingerprints = workflow(project, "fingerprint", {"references": refs})["fingerprints"]
    assert fingerprints[0]["changed"] is None and fingerprints[1]["changed"] is False
    (project / "spec.md").write_text((project / "spec.md").read_text() + "\nChanged.\n")
    previous = {key: value for key, value in fingerprints[0].items() if key != "changed"}
    assert workflow(project, "fingerprint", {"references": [previous]})["fingerprints"][0]["changed"]


@pytest.mark.parametrize("continue_on_failure", [False, True])
def test_check_batches_never_reexecute(project: Path, continue_on_failure: bool) -> None:
    planned(project)
    payload = {
        "id": "batch",
        "continue_on_failure": continue_on_failure,
        "checks": [
            {"task": "T01", "argv": [sys.executable, "-c", "print('failed'); raise SystemExit(3)"]},
            {"step": "report", "argv": [sys.executable, "-c", "print('report checked')"]},
        ],
    }
    result = workflow(project, "verify", payload)
    assert not result["passed"]
    assert result["omitted_checks"] == (0 if continue_on_failure else 1)
    with patch.object(lib.subprocess, "run", side_effect=AssertionError("must not rerun")):
        assert workflow(project, "verify", payload) == result
    record = result["attempts"][0]["result"]["record_id"]
    assert "failed" in workflow(project, "read", {"document": "evidence", "entry": record})["text"]


@pytest.mark.parametrize("mode", ["timeout", "launch_error", "rejected", "save_failure", "unknown"])
def test_check_attempt_recovery(project: Path, mode: str) -> None:
    planned(project)
    payload: dict[str, Any] = {
        "id": "attempt",
        "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('real result')"], "timeout": 300.0}],
    }
    if mode == "timeout":
        payload["checks"][0]["argv"] = [
            sys.executable,
            "-c",
            "import time; print('partial', flush=True); time.sleep(2)",
        ]
        payload["checks"][0]["timeout"] = 0.01
    elif mode == "launch_error":
        payload["checks"][0]["argv"] = [str(project / "absent-executable")]
    elif mode == "rejected":
        payload["checks"][0]["task"] = "unknown"
    if mode == "save_failure":
        with patch.object(lib, "_append_evidence_entry", side_effect=lib.WorkspaceError("disk full")):
            with pytest.raises(journal.OperationError) as failure:
                workflow(project, "verify", payload)
        assert failure.value.result["attempts"][0]["status"] == "persistable"
    elif mode == "unknown":
        with patch.object(lib.subprocess, "run", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                workflow(project, "verify", payload)
        with pytest.raises(journal.OperationError, match="unknown"):
            workflow(project, "verify", payload)
        return
    result = workflow(project, "verify", payload)
    assert result["attempts"][0]["status"] == ("recorded" if mode == "save_failure" else mode)
    with patch.object(lib.subprocess, "run", side_effect=AssertionError("must not rerun")):
        assert workflow(project, "verify", payload) == result


def test_metadata_partial_recovery_and_landed_commit(project: Path) -> None:
    fill_spec(project)
    payload = request(
        project,
        patch={"status": "PLANNING", "tasks": [task("T01")]},
        decision="Plan accepted",
        continuation={"next": "Start T01"},
    )
    original = journal.atomic_write_text

    def fail_handoff(path: Path, body: str) -> None:
        if path.name == "handoff.md":
            raise OSError("full")
        original(path, body)

    with patch.object(journal, "atomic_write_text", side_effect=fail_handoff):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "reconcile", payload)
    assert failure.value.result["saved"] == ["spec"]
    assert not failure.value.result["committed"]
    assert not workflow(project, "recover", {"id": "operation"})["complete"]
    with patch.object(journal, "_rebuild_index_after_commit", side_effect=lib.WorkspaceError("index full")):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "reconcile", payload)
    assert failure.value.result["committed"]
    assert _load_state(project)["revision"] == 1
    result = workflow(project, "recover", {"id": "operation", "apply": True})
    assert result["committed"] and result["revision"] == 1
    assert workflow(project, "recover", {"id": "operation", "apply": True}) == result
    assert workflow(project, "reconcile", payload) == result


def test_final_handoff_only_after_commit(project: Path) -> None:
    done(project)
    payload = request(
        project,
        "close",
        reflection="# Reflection\n\nVerified outcome; no remaining work.",
        continuation={"next": "None"},
    )
    with patch.object(journal, "_commit_state_locked", side_effect=lib.WorkspaceError("commit rejected")):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "finalize", payload)
    assert not failure.value.result["committed"]
    assert not (project / "handoff.md").exists()
    original = journal.atomic_write_text

    def fail_final(path: Path, text: str) -> None:
        if path.name == "handoff.md":
            raise OSError("note full")
        original(path, text)

    with patch.object(journal, "atomic_write_text", side_effect=fail_final):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "finalize", payload)
    assert failure.value.result["committed"]
    assert _load_state(project)["status"] == "DONE"
    workflow(project, "recover", {"id": "close", "apply": True})
    checkpoint = context.checkpoint_data(project)
    assert checkpoint is not None and checkpoint["phase"] == "DONE"


def test_memory_triage_assessment_and_report(project: Path) -> None:
    done(project)
    staging = project / "memory-staging.md"
    staging.write_text(
        "# Staging\n\n## Reuse\n\nA general lesson.\n\n## Local\n\nLocal detail.\n"
        "\n## Ignore\n\nDuplicate.\n\n## Open\n\nUnresolved.\n"
    )
    payload = request(
        project,
        "triage",
        items=[
            {
                "section": "Reuse",
                "disposition": "promote",
                "reason": "Reusable",
                "promotion": {
                    "topic": "parser-testing",
                    "body": "Check parser errors.",
                    "description": "Parser testing",
                    "scope": "Parsers",
                    "kind": "method",
                    "expected_sha256": "missing",
                },
            },
            {"section": "Local", "disposition": "reflection", "reason": "Local only"},
            {"section": "Ignore", "disposition": "discard", "reason": "Duplicate"},
        ],
    )
    payload["tokens"] = {
        name: token for name, token in payload["tokens"].items() if name in ("memory-staging", "reflection")
    }
    result = workflow(project, "triage", payload)
    assert len(result["receipts"]) == 1
    assert workflow(project, "triage", payload) == result
    assert "## Open" in staging.read_text() and "## Reuse" not in staging.read_text()
    assert "Local detail" in (project / "reflection.md").read_text()
    assert workflow(project, "memory-health", {})["status"] in ("available", "no_matches")
    assessment = workflow(
        project,
        "assess",
        request(
            project,
            "assess",
            topic="parser-testing",
            disposition="apply",
            reason="Same parser failure mode",
            application="Use malformed input tests",
        ),
    )
    assert assessment["topic"]["sha256"]
    result = workflow(project, "report", request(project, "report", graph=True, sections={}))
    assert result["unwritten_sections"] and not result["link_errors"]
    assert "Task graph" in (project / "artifacts/report.md").read_text()


@pytest.mark.parametrize("host", ["bin", "skills/project/scripts"])
@pytest.mark.parametrize("schema", [3, 4])
@pytest.mark.parametrize("runtime", ["default", "system"])
def test_cross_host_commands(tmp_path: Path, host: str, schema: int, runtime: str) -> None:
    launcher = REPO_ROOT / "plugins/research" / host / "research-project"
    environment = dict(os.environ)
    if runtime == "system":
        if not Path("/usr/bin/python3").exists():
            pytest.skip("system Python unavailable")
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    initialized = subprocess.run(
        [
            str(launcher),
            "init",
            str(tmp_path / "root"),
            "--create-root",
            "--json",
            "--title",
            "Host lifecycle",
            "--working-directory",
            str(tmp_path),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(initialized.stdout)
    project = Path(result["project_directory"])
    assert result["revision"] == 0 and result["documents"]["handoff"]["sha256"] == "missing"
    if schema == 3:
        state = _load_state(project)
        state["schema_version"] = 3
        state.pop("execution")
        lib.atomic_write_json(project / "project.json", state)
    payload = request(project, continuation={"next": "Ask requirements"})
    saved = subprocess.run(
        [str(launcher), "workflow", str(project), "checkpoint", "-"],
        input=json.dumps(payload),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(saved.stdout)["tokens"]["handoff"]
    resumed = subprocess.run(
        [str(launcher), "workflow", str(project), "resume", "-"],
        input="{}",
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(resumed.stdout)["schema_version"] == schema
    check_payload = {"id": "cross-check", "checks": [{"step": "report", "argv": ["python3", "-c", "print('checked')"]}]}
    checked = subprocess.run(
        [str(launcher), "workflow", str(project), "verify", "-"],
        input=json.dumps(check_payload),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(checked.stdout)["passed"]
    code, output, _ = invoke(
        ["update", str(project), "-", "--expected-revision", "0", "--dry-run"], stdin='{"title":"Preview"}'
    )
    assert code == 0 and json.loads(output)["project"]["title"]["after"] == "Preview"


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("resume", {"max_chars": 0}),
        ("resume", {"selections": [{"document": "spec", "max_chars": "bad"}]}),
        ("read", {"document": "spec", "section": "Current specification", "entry": "one"}),
        ("read", {"document": "spec", "entry": "absent"}),
        ("read", {"document": "spec", "offset": -1}),
        ("read", {"document": "../escape"}),
        ("fingerprint", {"references": [{"root": [], "path": "x"}]}),
        ("fingerprint", {"references": [{"root": "workspace", "path": "../escape"}]}),
        ("fingerprint", {"references": [{"root": "workspace", "path": "."}]}),
        ("recover", {"id": "x", "apply": "yes"}),
        ("unknown", {}),
        ("verify", {"id": "x", "checks": []}),
        ("verify", {"id": "x", "checks": [{"argv": ["true"]}]}),
        ("verify", {"id": "x", "checks": [{"task": "T01", "argv": ["true"], "timeout": 0}]}),
    ],
)
def test_invalid_read_and_check_inputs(project: Path, action: str, payload: dict[str, Any]) -> None:
    with pytest.raises(lib.WorkspaceError):
        workflow(project, action, payload)


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("checkpoint", {"continuation": {"next": "Next", "session": "done"}}),
        ("checkpoint", {"continuation": {"next": 1}}),
        ("checkpoint", {"continuation": {}}),
        ("confirm", {"kind": "delivery"}),
        ("confirm", {"kind": "requirements", "proposal_sha256": "stale"}),
        ("correct", {"task": "T01", "replacement": {}, "reason": "No"}),
        ("maintenance", {"reason": "Not completed"}),
        ("worker-event", {"task": "T01", "handle": "h", "scope": "s", "observation": "o", "event": "invalid"}),
        (
            "worker-event",
            {
                "task": "T01",
                "handle": "h",
                "scope": "s",
                "observation": "o",
                "event": "launched",
                "assignment_revision": 100,
            },
        ),
        ("report", {"graph": "yes"}),
    ],
)
def test_invalid_metadata_inputs(project: Path, action: str, fields: dict[str, Any]) -> None:
    planned(project)
    with pytest.raises(lib.WorkspaceError):
        workflow(project, action, request(project, **fields))


def test_cli_automation_results(project: Path, tmp_path: Path) -> None:
    code, output, _ = invoke(
        [
            "init",
            str(tmp_path / "second"),
            "--create-root",
            "--json",
            "--title",
            "Test",
            "--working-directory",
            str(tmp_path),
        ]
    )
    assert code == 0 and json.loads(output)["revision"] == 0
    for action, payload in (("freshness", {}), ("readiness", {})):
        code, output, _ = invoke(["workflow", str(project), action, "-"], stdin=json.dumps(payload))
        assert output and code == (1 if action == "readiness" else 0)
    code, _, error = invoke(
        ["workflow", str(project), "checkpoint", "-"],
        stdin=json.dumps(request(project, expected_revision=10, continuation={"next": "x"})),
    )
    assert code == 1 and "expected_revision" in error


def test_legacy_checkpoint_git_and_malformed_metadata(project: Path) -> None:
    note = project / "handoff.md"
    note.write_text("Keep this unresolved question.\n")
    with pytest.raises(lib.WorkspaceError, match="legacy"):
        workflow(project, "checkpoint", request(project, continuation={"next": "Answer"}))
    with patch.object(context, "git_observation", return_value={"available": True, "commit": "abc"}):
        workflow(
            project, "checkpoint", request(project, "import", continuation={"next": "Answer", "import_legacy": True})
        )
        workflow(project, "checkpoint", request(project, "again", continuation={"next": "Investigate"}))
        assert "Keep this unresolved question" in note.read_text()
        assert workflow(project, "freshness", {})["target"] == "unchanged"
    with patch.object(context.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "x" * 5000)):
        assert context.git_observation(project)["dirty_truncated"]
    with patch.object(context.subprocess, "run", side_effect=OSError("git unavailable")):
        assert context.git_observation(project) == {"available": False}
    for value in (
        "{",
        '{"version":2,"revision":0,"sources":{},"continuation":{},"git":{}}',
        '{"version":1,"revision":0,"sources":{"spec":42},"continuation":{},"git":{}}',
    ):
        note.write_text("<!-- research-checkpoint-v1\n" + value + "\n-->\n")
        with pytest.raises(lib.WorkspaceError):
            context.checkpoint_data(project)


def test_memory_health_and_small_input_guards(project: Path, tmp_path: Path) -> None:
    import memory_search

    assert memory_search.memory_health(tmp_path, "x")["status"] == "absent"
    with patch.object(memory_search, "load_indexed_topics", side_effect=lib.WorkspaceError("broken index")):
        assert workflow(project, "memory-health", {})["status"] == "failed"
    for value in (None, {}, "not-array", list(range(51))):
        with pytest.raises(lib.WorkspaceError):
            journal.list_input(value)
    with pytest.raises(lib.WorkspaceError):
        journal.journal_path(project, "Invalid")
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(lib.WorkspaceError, match="escapes"):
        journal.contained(project, "escape/file")
    with patch.object(context, "snapshot", side_effect=OSError("unreadable")):
        with pytest.raises(lib.WorkspaceError):
            workflow(project, "freshness", {})
    with pytest.raises(lib.WorkspaceError, match="active"):
        journal.ensure_writable({"execution": {"coordinator_run": "active"}})


@pytest.mark.parametrize("change", ["revision", "document", "identity", "tokens", "candidate"])
def test_metadata_conflicts(project: Path, change: str) -> None:
    fill_spec(project)
    payload = request(project, continuation={"next": "Proceed"})
    if change == "tokens":
        payload["tokens"] = []
    elif change == "candidate":
        with pytest.raises(lib.WorkspaceError):
            workflow(
                project,
                "reconcile",
                request(project, patch={"status": "DONE"}, decision="Not ready", continuation={"next": "No"}),
            )
        return
    else:
        with patch.object(journal, "_advance", side_effect=lib.WorkspaceError("interrupted")):
            with pytest.raises(journal.OperationError):
                workflow(project, "checkpoint", payload)
        if change == "revision":
            update_project_data(project, {"title": "Changed"}, expected_revision=0)
        elif change == "document":
            (project / "handoff.md").write_text("Someone else's note")
        else:
            payload["continuation"] = {"next": "Different"}
    with pytest.raises(lib.WorkspaceError):
        workflow(project, "checkpoint", payload)


def test_tokens_and_corrupt_journal_guards(project: Path) -> None:
    with pytest.raises(lib.WorkspaceError, match="document conflict"):
        workflow(project, "round", request(project, tokens={}, sections={"Current specification": "Changed"}))
    payload = request(project, continuation={"next": "Proceed"})
    workflow(project, "checkpoint", payload)
    path = journal.journal_path(project, "operation")
    original = lib.load_json(path)
    for fields in (
        {"documents": []},
        {"documents": {"handoff": {"before": "missing", "text": 1}}},
        {"saved": ["unknown"]},
    ):
        lib.atomic_write_json(path, original | fields)
        with pytest.raises(lib.WorkspaceError):
            workflow(project, "recover", {"id": "operation", "apply": True})
    lib.atomic_write_json(path, {"kind": "checks"})
    with pytest.raises(lib.WorkspaceError, match="own same-ID"):
        workflow(project, "recover", {"id": "operation", "apply": True})


def test_check_journal_conflicts_and_os_launch_error(project: Path) -> None:
    planned(project)
    payload = {"id": "check", "checks": [{"task": "T01", "argv": ["chosen-check"]}]}
    with patch.object(lib.subprocess, "run", side_effect=PermissionError("not executable")):
        assert workflow(project, "verify", payload)["attempts"][0]["status"] == "launch_error"
    with pytest.raises(lib.WorkspaceError, match="different input"):
        workflow(project, "verify", payload | {"continue_on_failure": True})
    path = journal.journal_path(project, "check")
    original = lib.load_json(path)
    for attempt in (
        {"index": 0, "status": "unknown"},
        {"index": 0, "status": "recorded"},
        {"index": 0, "status": "recorded", "result": {"exit_code": 0}, "lines": False},
    ):
        lib.atomic_write_json(path, original | {"attempts": [attempt]})
        with pytest.raises(lib.WorkspaceError, match="damaged"):
            workflow(project, "verify", payload)


def test_additional_record_guards_and_citations(project: Path) -> None:
    from workspace_reports import _citation

    planned(project)
    payload = request(project, decisions=[{"id": "one", "body": "Decision"}])
    workflow(project, "round", payload)
    with pytest.raises(lib.WorkspaceError, match="entry ID"):
        workflow(project, "round", request(project, "new", decisions=payload["decisions"]))
    for sections in ([], {"Current specification": []}):
        with pytest.raises(lib.WorkspaceError, match="headings"):
            workflow(project, "round", request(project, "sections", sections=sections))
    notes = project / "tasks/T01.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text("<!-- research-worker { -->\n")
    with pytest.raises(lib.WorkspaceError, match="worker event"):
        workflow(project, "resume", {})
    (project / "architecture.md").write_text("# A1\n\nNo status")
    with pytest.raises(lib.WorkspaceError, match="declare its status"):
        workflow(
            project,
            "confirm",
            request(
                project,
                "confirm",
                kind="architecture",
                review_id="A1",
                source="User",
                scope="A1",
                response="Approved",
                proposal_sha256=journal.snapshot(project / "architecture.md")[1],
            ),
        )
    lib.amend_memory_topic(project.parent, "example", body="Rule", description="Example", kind="method", scope="Tests")
    with pytest.raises(lib.WorkspaceError, match="disposition"):
        workflow(project, "assess", request(project, "assess", topic="example", disposition="invalid"))
    state = _load_state(project)
    assert _citation(project, state, {"root": "workspace", "path": "missing", "anchor": None})[1]
    assert _citation(project, state, {"root": "workspace", "path": "spec.md", "anchor": "missing"})[1]


def test_memory_promotion_idempotency_and_conflicts(project: Path) -> None:
    args: dict[str, Any] = {"body": "Actual rule", "description": "Example", "kind": "method", "scope": "Tests"}
    with pytest.raises(lib.WorkspaceError, match="entry ID"):
        lib.amend_memory_topic(project.parent, "example", entry_id="Bad", **args)
    path = lib.amend_memory_topic(project.parent, "example", entry_id="one", expected_sha256="missing", **args)
    before = path.read_bytes()
    assert lib.amend_memory_topic(project.parent, "example", entry_id="one", expected_sha256="missing", **args) == path
    assert path.read_bytes() == before
    with pytest.raises(lib.WorkspaceError, match="different content"):
        changed: dict[str, Any] = {**args, "body": "Other"}
        lib.amend_memory_topic(project.parent, "example", entry_id="one", **changed)
    with pytest.raises(lib.WorkspaceError, match="changed"):
        lib.amend_memory_topic(project.parent, "example", expected_sha256="stale", **args)


@pytest.mark.parametrize(
    "mode", ["long", "empty", "revision", "token", "duplicate", "nested", "disposition", "promotion", "reflection"]
)
def test_triage_preflight_refusals(project: Path, mode: str) -> None:
    staging = project / "memory-staging.md"
    staging.write_text("# Staging\n\n## Item\n\nUseful lesson\n")
    payload = request(project, "triage", items=[{"section": "Item", "disposition": "discard", "reason": "Duplicate"}])
    payload["tokens"] = {"memory-staging": journal.snapshot(staging)[1]}
    if mode == "long":
        payload["id"] = "x" * 46
    elif mode == "empty":
        payload["items"] = []
    elif mode == "revision":
        payload["expected_revision"] = 10
    elif mode == "token":
        payload["tokens"] = {}
    elif mode == "duplicate":
        payload["items"] *= 2
    elif mode == "nested":
        staging.write_text(staging.read_text() + "\n### Nested\n\nChild\n")
        payload["tokens"]["memory-staging"] = journal.snapshot(staging)[1]
    elif mode == "disposition":
        payload["items"][0]["disposition"] = "invalid"
    elif mode == "promotion":
        payload["items"][0]["promotion"] = {}
    else:
        payload["items"][0]["disposition"] = "reflection"
        payload["tokens"]["reflection"] = "stale"
    with pytest.raises(lib.WorkspaceError):
        workflow(project, "triage", payload)


def test_triage_uncertain_promotion_receipt_does_not_duplicate(project: Path) -> None:
    import workspace_lessons

    staging = project / "memory-staging.md"
    staging.write_text("# Staging\n\n## Item\n\nUseful lesson\n")
    payload = request(
        project,
        "triage",
        items=[
            {
                "section": "Item",
                "disposition": "promote",
                "reason": "Reusable",
                "promotion": {
                    "topic": "example",
                    "body": "Rule",
                    "description": "Example",
                    "kind": "method",
                    "scope": "Tests",
                    "expected_sha256": "missing",
                },
            }
        ],
    )
    payload["tokens"] = {"memory-staging": journal.snapshot(staging)[1]}
    original = workspace_lessons.atomic_write_json

    def fail_receipt(path: Path, value: dict[str, Any]) -> None:
        if value.get("receipts"):
            raise OSError("receipt persistence failed")
        original(path, value)

    with patch.object(workspace_lessons, "atomic_write_json", side_effect=fail_receipt):
        with pytest.raises(journal.OperationError):
            workflow(project, "triage", payload)
    topic = project.parent / "memory/example.md"
    before = topic.read_bytes()
    with pytest.raises(lib.WorkspaceError, match="different input"):
        workflow(project, "triage", payload | {"expected_revision": 10})
    workflow(project, "triage", payload)
    assert topic.read_bytes() == before and "## Item" not in staging.read_text()
    path = journal.journal_path(project, "triage")
    original_journal = lib.load_json(path)
    for change in ({"documents": []}, {"documents": {"spec": "forbidden"}}):
        lib.atomic_write_json(path, original_journal | change)
        with pytest.raises(lib.WorkspaceError, match="damaged memory"):
            workflow(project, "triage", payload)


def test_final_note_recovery_conflicts(project: Path) -> None:
    done(project)
    payload = request(project, reflection="# Reflection\n\nOutcome verified.", continuation={"next": "None"})
    original = journal.atomic_write_text

    def fail_note(path: Path, text: str) -> None:
        if path.name == "handoff.md":
            raise OSError("full")
        original(path, text)

    with patch.object(journal, "atomic_write_text", side_effect=fail_note):
        with pytest.raises(journal.OperationError):
            workflow(project, "finalize", payload)
    (project / "handoff.md").write_text("Another session's note")
    with pytest.raises(lib.WorkspaceError, match="changed"):
        workflow(project, "recover", {"id": "operation", "apply": True})
    update_project_data(project, {"title": "New title"}, expected_revision=5)
    with pytest.raises(lib.WorkspaceError, match="state advanced"):
        workflow(project, "recover", {"id": "operation", "apply": True})


def test_lost_save_acknowledgement_and_final_index_recovery(project: Path) -> None:
    original = journal.atomic_write_text
    payload = request(project, continuation={"next": "Plan"})

    def lost_ack(path: Path, text: str) -> None:
        original(path, text)
        raise OSError("write landed but acknowledgement failed")

    with patch.object(journal, "atomic_write_text", side_effect=lost_ack):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "checkpoint", payload)
    assert failure.value.result["saved"] == ["handoff"]
    workflow(project, "checkpoint", payload)
    done(project)
    final = request(project, "final", reflection="# Reflection\n\nVerified result.", continuation={"next": "None"})
    with patch.object(journal, "_rebuild_index_after_commit", side_effect=lib.WorkspaceError("index failed")):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "finalize", final)
    assert failure.value.result["committed"]
    workflow(project, "recover", {"id": "final", "apply": True})


def test_evidence_saved_before_lost_ack_is_not_duplicated(project: Path) -> None:
    import workspace_checks

    planned(project)
    payload = {"id": "batch", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('checked')"]}]}
    original = workspace_checks.atomic_write_json

    def lose_ack(path: Path, value: dict[str, Any]) -> None:
        if value.get("attempts") and value["attempts"][0]["status"] == "recorded":
            raise OSError("lost acknowledgement")
        original(path, value)

    with patch.object(workspace_checks, "atomic_write_json", side_effect=lose_ack):
        with pytest.raises(journal.OperationError):
            workflow(project, "verify", payload)
    before = (project / "evidence.md").read_bytes()
    assert workflow(project, "verify", payload)["passed"]
    assert (project / "evidence.md").read_bytes() == before


def test_bounded_fingerprints_and_closed_inputs(project: Path) -> None:
    from io import BytesIO

    with pytest.raises(lib.WorkspaceError, match="object fields"):
        workflow(project, "read", {"document": "spec", "unknown": 1})
    state = _load_state(project)
    with (
        patch.object(context, "_load_state", return_value=state),
        patch.object(Path, "open", return_value=BytesIO(b"x" * (16 * 1024 * 1024 + 1))),
    ):
        with pytest.raises(lib.WorkspaceError, match="grew"):
            workflow(project, "fingerprint", {"references": [{"root": "workspace", "path": "spec.md"}]})


@pytest.mark.parametrize("history", [0, 200])
def test_bundled_resume_call_and_payload_budget(project: Path, history: int) -> None:
    planned(project)
    (project / "architecture.md").write_text("# A1\n\nStatus: agreed\n\n## Interfaces\n\nInput is JSON.\n")
    spec = project / "spec.md"
    spec.write_text(spec.read_text() + "\nHistorical decision, no current authority.\n" * history)
    workflow(project, "checkpoint", request(project, continuation={"next": "Verify T01", "ownership": "No workers"}))
    check = workflow(
        project,
        "verify",
        {"id": "check", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('verified')"]}]},
    )
    record = check["attempts"][0]["result"]["record_id"]
    before_commands = [
        ["context", str(project), "--validate"],
        ["read", str(project), "handoff"],
        ["read", str(project), "architecture"],
        ["context", str(project), "--task", "T01", "--task-only"],
        ["read", str(project), "evidence", "--entry", record],
    ]
    before = [invoke(command) for command in before_commands]
    payload = {
        "task": "T01",
        "selections": [
            {"document": "handoff"},
            {"document": "spec", "section": "Current specification"},
            {"document": "architecture"},
            {"document": "evidence", "entry": record},
        ],
    }
    after = invoke(["workflow", str(project), "resume", "-"], stdin=json.dumps(payload))
    assert all(item[0] == 0 for item in before) and after[0] == 0
    before_bytes = sum(len(item[1].encode()) for item in before)
    after_bytes = len(after[1].encode())
    assert after_bytes < before_bytes
    actual = json.loads(after[1])
    assert actual["assignment"]["selected_task"] == json.loads(before[3][1])["selected_task"]
    assert all(not item["truncated"] for item in actual["sources"])
    assert "verified" in actual["sources"][-1]["text"]
    print(
        json.dumps(
            {
                "history": history,
                "before_calls": 5,
                "after_calls": 1,
                "before_output_bytes": before_bytes,
                "after_output_bytes": after_bytes,
            }
        )
    )
