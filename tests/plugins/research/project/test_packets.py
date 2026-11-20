"""Worker packets (T04): reference resolution, packet assembly, `entry_body` and `packet_sha256`."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import workspace_packets
from workspace_documents import append_record, entry_body, friction_entries
from workspace_durability import task_attempts, worker_lines
from workspace_lib import WorkspaceError, validate_project
from workspace_packets import PACKET_WARN_CHARS, build_packet, reference_errors, spec_sections
from workspace_session import project_context, update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_policy_five import _project as _policy_project

HEADING = "Objective and audience"
OTHER = "In scope"


def _project(tmp_path: Path, policy: int) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _policy_project(tmp_path, policy)


def _load_state(project: Path) -> dict[str, Any]:
    """The stored state as is: some tests hold a state that `_load_state` of the tool would refuse."""
    return json.loads((project / "project.json").read_text(encoding="utf-8"))


def _set(project: Path, task_id: str, **fields: Any) -> None:
    """Write task fields straight into project.json, so a test can hold a state the commit hook refuses."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    next(task for task in state["tasks"] if task["id"] == task_id).update(fields)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _spec(project: Path) -> str:
    return (project / "spec.md").read_text(encoding="utf-8")


def _intent(project: Path, identity: str, **extra: Any) -> dict[str, Any]:
    state = _load_state(project)
    return workflow(
        project,
        "worker-event",
        request(
            project, identity, task="T01", handle="w", event="intent", scope="s", observation="o",
            assignment_revision=state["revision"], **extra,
        ),
    )


# --- entry_body -----------------------------------------------------------------------------------


def test_entry_body_reads_one_decision_and_checks_its_digest(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "First body.\n\nSecond paragraph.", entry_id="q1")
    append_record(project, "decision", "Other body.", entry_id="q2")
    found = entry_body(_spec(project), "q1")
    assert found == {"body": "First body.\n\nSecond paragraph.", "digest_ok": True}
    assert entry_body(_spec(project), "q2")["body"] == "Other body."


def test_entry_body_reports_a_digest_mismatch_and_keeps_the_body(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "Original.", entry_id="q1")
    text = _spec(project).replace("Original.", "Edited by hand.")
    assert entry_body(text, "q1") == {"body": "Edited by hand.", "digest_ok": False}


def test_entry_body_refuses_unknown_and_repeated_ids(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "Body.", entry_id="q1")
    with pytest.raises(WorkspaceError, match="no decision entry with ID: nope"):
        entry_body(_spec(project), "nope")
    with pytest.raises(WorkspaceError, match="more than once: q1"):
        entry_body(_spec(project) + "\n<!-- research-entry: q1 -->\nagain\n", "q1")


def test_entry_body_ignores_a_marker_inside_a_fenced_block(tmp_path: Path) -> None:
    text = (
        "<!-- research-entry: q1 -->\n### 2026-11-18T10:00:00+01:00 — q1\n\nBody.\n\n"
        "```text\n<!-- research-entry: q2 -->\nquoted\n```\n"
    )
    assert entry_body(text, "q1")["body"].endswith("```")  # the fenced marker did not end the entry
    with pytest.raises(WorkspaceError, match="no decision entry"):
        entry_body(text, "q2")
    assert [e["id"] for e in friction_entries(text)] == ["q1", "q2"]  # friction still splits on every marker


def test_entry_body_of_an_unfinished_fence_runs_to_the_end() -> None:
    text = "intro\n```\n<!-- research-entry: q1 -->\n"
    with pytest.raises(WorkspaceError, match="no decision entry"):
        entry_body(text, "q1")


def test_entry_body_without_a_heading_line_uses_the_whole_chunk() -> None:
    assert entry_body("<!-- research-entry: q1 -->\nplain\n", "q1") == {"body": "plain", "digest_ok": False}


# --- spec_sections ---------------------------------------------------------------------------------


def test_spec_sections_split_on_level_three_and_keep_children() -> None:
    text = (
        "## Current specification\n\n### A\nalpha\n#### child\nkept\n\n### B\nbeta\n\n"
        "## Decision history\n\n### C\nnot a spec section\n"
    )
    sections, duplicates = spec_sections(text)
    assert list(sections) == ["A", "B"] and duplicates == []
    assert "kept" in sections["A"] and sections["A"].startswith("### A")
    assert "beta" not in sections["A"] and "not a spec" not in sections["B"]


def test_spec_sections_report_duplicates_once_and_tolerate_a_missing_section() -> None:
    text = "## Current specification\n\n### A\none\n\n### A\ntwo\n\n### A\nthree\n\n### B\n"
    sections, duplicates = spec_sections(text)
    assert duplicates == ["A"] and sections["A"].endswith("one\n\n") and sections["B"] == "### B\n"
    assert spec_sections("## Something else\n### A\n") == ({}, [])


def test_spec_sections_run_to_the_end_of_the_text_when_it_ends_the_specification() -> None:
    sections, _ = spec_sections("## Current specification\n\n### A\nlast\n")
    assert sections == {"A": "### A\nlast\n"}


# --- reference_errors ------------------------------------------------------------------------------


def test_reference_errors_are_empty_below_policy_five(tmp_path: Path) -> None:
    project = _project(tmp_path, 4)
    assert reference_errors(project, _load_state(project)) == []


def test_a_task_needs_a_spec_reference_and_each_one_must_resolve(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    state = _load_state(project)
    errors = reference_errors(project, state)
    assert any(e.startswith("task T01: spec_refs needs at least one") for e in errors)
    _set(project, "T01", spec_refs=[HEADING, "Renamed heading"], decision_refs=["nope"])
    _set(project, "T02", spec_refs=[OTHER])
    errors = reference_errors(project, _load_state(project))
    assert len(errors) == 2
    assert "task T01: spec_refs names no `###` heading" in errors[0] and "Renamed heading" in errors[0]
    assert "task T01: decision_refs no decision entry with ID: nope" in errors[1]
    assert reference_errors(project, _load_state(project), ["T02"]) == []


def test_done_and_skipped_tasks_are_never_checked(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", status="DONE", spec_refs=["gone"])
    _set(project, "T02", status="SKIPPED")
    assert reference_errors(project, _load_state(project)) == []


def test_a_duplicate_heading_is_one_project_level_error(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    _set(project, "T02", spec_refs=[HEADING])
    text = _spec(project).replace(f"### {OTHER}", f"### {HEADING}", 1)
    (project / "spec.md").write_text(text, encoding="utf-8")
    errors = reference_errors(project, _load_state(project))
    assert [e for e in errors if e.startswith("spec.md:")] == [
        f"spec.md: two `###` headings under `## Current specification` read `{HEADING}`"
    ]


def test_a_missing_or_undecodable_spec_is_one_error_per_task_with_references(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    (project / "spec.md").unlink()
    errors = reference_errors(project, _load_state(project))
    assert [e for e in errors if "cannot be checked: spec.md is missing" in e] == [
        "task T01: spec_refs and decision_refs cannot be checked: spec.md is missing"
    ]
    (project / "spec.md").write_bytes(b"\xff\xfe\x00bad")
    assert any("cannot be checked" in e for e in reference_errors(project, _load_state(project)))


def test_malformed_reference_values_read_as_empty(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs="not a list", decision_refs=[1, ""])
    errors = reference_errors(project, _load_state(project), ["T01"])
    assert errors == ["task T01: spec_refs needs at least one specification heading"]


def test_the_commit_checks_only_the_tasks_it_touches(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=["stale"])  # a stale reference in a task a later commit never touches
    revision = _load_state(project)["revision"]
    update_project_data(project, {"title": "Renamed"}, expected_revision=revision)  # touches no task
    revision += 1
    with pytest.raises(WorkspaceError, match="task T02: spec_refs names no"):
        update_project_data(project, {"tasks": [{"id": "T02", "spec_refs": ["nope"]}]}, expected_revision=revision)
    result = update_project_data(
        project, {"tasks": [{"id": "T02", "spec_refs": [HEADING]}]}, expected_revision=revision
    )
    assert result["revision"] == revision + 1
    assert _load_state(project)["tasks"][1]["spec_refs"] == [HEADING]
    # a new task must carry resolving references in the same patch
    new = {
        "id": "T09", "name": "New", "success_criteria": "ok", "verification": "run", "depends_on": [],
        "effect": {"kind": "none", "description": None}, "outputs": [],
    }
    with pytest.raises(WorkspaceError, match="task T09: spec_refs needs at least one"):
        update_project_data(project, {"tasks": [new]}, expected_revision=revision + 1)
    update_project_data(project, {"tasks": [{**new, "spec_refs": [HEADING]}]}, expected_revision=revision + 1)


def test_validate_scans_every_open_task_at_policy_five_only(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=["stale"])
    _set(project, "T02", spec_refs=[HEADING])
    assert any("task T01: spec_refs names no" in e for e in validate_project(project).errors)
    _set(project, "T01", spec_refs=[HEADING])
    assert not any("spec_refs" in e for e in validate_project(project).errors)
    older = _project(tmp_path / "older", 4)
    assert not any("spec_refs" in e for e in validate_project(older).errors)


# --- build_packet and the context --worker surface -------------------------------------------------


def test_packet_holds_sections_decisions_hash_and_the_task_context(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "Use A, not B.", entry_id="q1")
    _set(project, "T01", spec_refs=[OTHER, HEADING, OTHER], decision_refs=["q1", "q1"])
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    sections, _ = spec_sections(_spec(project))
    assert [s["heading"] for s in packet["spec_sections"]] == [OTHER, HEADING]
    expected = hashlib.sha256(
        "\n\n".join(sections[name].rstrip() for name in (OTHER, HEADING)).encode("utf-8")
    ).hexdigest()
    assert packet["packet_sha256"] == expected
    assert packet["decisions"] == [{"id": "q1", "body": "Use A, not B."}]
    assert packet["acceptance"] == {"success_criteria": "ok", "verification": "run"}
    assert packet["write_scope"]["outputs"] == [] and packet["prohibited_effects"]["effect"]["kind"] == "external"
    assert packet["assignment_counter"] == 1 and packet["result_format"] == "references/task-workers.md"
    assert packet["chars"] == sum(len(s["text"]) for s in packet["spec_sections"]) + len("Use A, not B.")
    assert packet["notes"] == [] and packet["warnings"] == []


def test_the_hash_ignores_decision_appends_and_follows_the_referenced_text(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    state = _load_state(project)
    first = build_packet(project, state, state["tasks"][0])["packet_sha256"]
    append_record(project, "decision", "Unrelated.", entry_id="q9")
    assert build_packet(project, state, state["tasks"][0])["packet_sha256"] == first
    (project / "spec.md").write_text(_spec(project).replace(f"Content for {HEADING}.", "Changed."), encoding="utf-8")
    assert build_packet(project, state, state["tasks"][0])["packet_sha256"] != first


def test_a_large_packet_warns_and_is_never_truncated(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    big = "x" * (PACKET_WARN_CHARS + 1)
    (project / "spec.md").write_text(_spec(project).replace(f"Content for {HEADING}.", big), encoding="utf-8")
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    assert big in packet["spec_sections"][0]["text"]
    assert any("above 12000" in w for w in packet["warnings"])


def test_a_digest_mismatch_warns_and_keeps_the_decision(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "Original.", entry_id="q1")
    (project / "spec.md").write_text(_spec(project).replace("Original.", "Edited."), encoding="utf-8")
    _set(project, "T01", spec_refs=[HEADING], decision_refs=["q1"])
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    assert packet["decisions"] == [{"id": "q1", "body": "Edited."}]
    assert packet["warnings"] == ["decision q1: body does not match its research-body-sha256 line"]


def test_a_packet_refuses_a_bad_reference_but_a_done_task_drops_it_with_a_warning(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING, "gone"], decision_refs=["nope"])
    state = _load_state(project)
    with pytest.raises(WorkspaceError, match="spec_refs names no"):
        build_packet(project, state, state["tasks"][0])
    _set(project, "T01", status="DONE")
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    assert [s["heading"] for s in packet["spec_sections"]] == [HEADING]
    assert packet["warnings"] == [
        "spec_refs names no heading now, dropped: gone", "decision_refs names no entry now, dropped: nope",
    ]


def test_a_packet_refuses_a_duplicate_heading_and_a_missing_spec(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    state = _load_state(project)
    good = _spec(project)
    (project / "spec.md").write_text(good.replace(f"### {OTHER}", f"### {HEADING}", 1), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="two `###` headings"):
        build_packet(project, state, state["tasks"][0])
    (project / "spec.md").unlink()
    with pytest.raises(WorkspaceError, match=r"spec\.md is missing"):
        build_packet(project, state, state["tasks"][0])


def test_context_worker_carries_the_packet_at_policy_five_only(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    context = project_context(project, task_id="T01", worker=True)
    assert context["packet"]["spec_sections"][0]["heading"] == HEADING
    assert context["validation"].startswith("references checked against the current spec.md")
    assert context["context_required"].startswith("The packet carries")
    coordinator = project_context(project, task_id="T01", task_only=True)
    assert "packet" not in coordinator and coordinator["validation"].startswith("structure only")
    assert coordinator["context_required"].startswith("Coordinator must supply")
    older = _project(tmp_path / "older", 4)
    old = project_context(older, task_id="T01", worker=True)
    assert "packet" not in old and old["validation"].startswith("structure only")


# --- packet_sha256 on a worker event ---------------------------------------------------------------


def _lines(project: Path) -> list[dict[str, Any]]:
    text = (project / "tasks/T01.md").read_text(encoding="utf-8")
    return [json.loads(m) for m in re.findall(r"^<!-- research-worker (.+) -->$", text, re.MULTILINE)]


def test_an_intent_event_stores_packet_sha256(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    digest = "ab" * 32
    _intent(project, "intent-1", packet_sha256=digest)
    (line,) = _lines(project)
    assert line["packet_sha256"] == digest and line["attempt_id"].startswith("T01-1-")
    # without the field nothing is stored
    other = _project(tmp_path / "plain", 5)
    _intent(other, "intent-1")
    assert "packet_sha256" not in _lines(other)[0]


@pytest.mark.parametrize("value", ["AB" * 32, "ab" * 31, "zz" * 32, 7])
def test_a_malformed_packet_hash_is_refused(tmp_path: Path, value: object) -> None:
    project = _project(tmp_path, 5)
    with pytest.raises(WorkspaceError):
        _intent(project, "intent-1", packet_sha256=value)


def test_packet_sha256_belongs_to_an_intent_event_at_policy_five(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    attempt = _intent(project, "intent-1")["attempt_id"]
    state = _load_state(project)
    with pytest.raises(WorkspaceError, match="belongs to an intent event"):
        workflow(
            project, "worker-event",
            request(
                project, "obs-1", task="T01", handle="w", event="observed", scope="s", observation="o",
                assignment_revision=state["revision"], attempt_id=attempt, packet_sha256="ab" * 32,
            ),
        )
    older = _project(tmp_path / "older", 4)
    with pytest.raises(WorkspaceError, match="packet_sha256 needs policy_version 5"):
        _intent(older, "intent-1", packet_sha256="ab" * 32)
    three = _project(tmp_path / "three", 3)
    with pytest.raises(WorkspaceError, match="packet_sha256 needs policy_version 5"):
        _intent(three, "intent-1", packet_sha256="ab" * 32)


def test_damaged_packet_hash_lines_are_skipped_with_a_warning_at_policy_five() -> None:
    def line(**fields: Any) -> str:
        event = {
            "task": "T01", "handle": "w", "event": "intent", "scope": "s", "observation": "o",
            "assignment_revision": 1, "attempt_id": "T01-1-1", "counter": 1, "recorded": "now", **fields,
        }
        return "<!-- research-worker " + json.dumps(event) + " -->\n"

    good = line(packet_sha256="ab" * 32)
    text = good + line(packet_sha256="short") + line(event="observed", packet_sha256="ab" * 32)
    warnings: list[str] = []
    assert len(worker_lines(text, True, warnings, "T01", True)) == 1
    assert len(warnings) == 2 and all("packet_sha256" in w for w in warnings)
    # below policy 5 the key is unknown, so the line is damaged
    warnings = []
    assert worker_lines(good, True, warnings, "T01", False) == [] and "damaged" in warnings[0]


def test_a_changed_packet_hash_is_a_note_not_a_bump(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    assert packet["notes"] == []  # no intent yet
    _intent(project, "intent-1", packet_sha256=packet["packet_sha256"])
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][0])["notes"] == []
    (project / "spec.md").write_text(_spec(project).replace(f"Content for {HEADING}.", "Changed."), encoding="utf-8")
    again = build_packet(project, state, state["tasks"][0])
    assert len(again["notes"]) == 1 and "T01-1-1" in again["notes"][0]
    assert _load_state(project)["tasks"][0]["assignment_counter"] == 1


def test_a_task_without_notes_or_an_intent_hash_adds_no_note(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T02", spec_refs=[HEADING])
    state = _load_state(project)
    assert not (project / "tasks/T02.md").exists()
    assert build_packet(project, state, state["tasks"][1])["notes"] == []
    assert task_attempts(project, state, "T01") == ([], {}, [])


# --- delegating: the two warnings it governs --------------------------------------------------------


def _delegating(project: Path, on: bool) -> None:
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["options"] = {"delegating": on}
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def test_delegating_warns_for_a_dependency_that_is_not_done(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T02", spec_refs=[HEADING], depends_on=["T01"])  # T01 is RUNNING
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == []  # off: never added
    _delegating(project, True)
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == ["dependency T01 is RUNNING, not DONE"]
    _set(project, "T01", status="SKIPPED")
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == ["dependency T01 is SKIPPED, not DONE"]
    _set(project, "T01", status="DONE")
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == []


def test_delegating_turns_a_changed_packet_hash_into_a_warning(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    _intent(project, "intent-1", packet_sha256="ab" * 32)
    state = _load_state(project)
    off = build_packet(project, state, state["tasks"][0])
    assert len(off["notes"]) == 1 and off["warnings"] == []
    _delegating(project, True)
    on = build_packet(project, _load_state(project), _load_state(project)["tasks"][0])
    assert on["warnings"] == on["notes"] and len(on["notes"]) == 1


# --- review fixes: terminal duplicates, open attempts and the spec-edit warning --------------------------


def test_a_done_task_is_not_stranded_by_a_duplicate_heading(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING], status="DONE")
    (project / "spec.md").write_text(_spec(project).replace(f"### {OTHER}", f"### {HEADING}", 1), encoding="utf-8")
    state = _load_state(project)
    packet = build_packet(project, state, state["tasks"][0])
    assert [s["heading"] for s in packet["spec_sections"]] == [HEADING]
    assert packet["warnings"] == [f"spec.md has two `###` headings that read `{HEADING}`; the first is used"]


def test_the_packet_lists_the_open_attempts_of_the_task(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    state = _load_state(project)
    assert build_packet(project, state, state["tasks"][0])["partial_work"]["open_attempts"] == []
    attempt = _intent(project, "intent-1")["attempt_id"]
    packet = build_packet(project, state, state["tasks"][0])
    assert packet["partial_work"]["task_notes"] == "tasks/T01.md"
    assert [(a["attempt_id"], a["counter"], a["event"]) for a in packet["partial_work"]["open_attempts"]] == [
        (attempt, 1, "intent")
    ]
    workflow(
        project, "worker-event",
        request(
            project, "stop-1", task="T01", handle="w", event="stopped", scope="s", observation="o",
            assignment_revision=_load_state(project)["revision"], attempt_id=attempt,
        ),
    )
    assert build_packet(project, state, state["tasks"][0])["partial_work"]["open_attempts"] == []


def test_a_spec_edit_warns_about_references_it_leaves_dangling(tmp_path: Path) -> None:
    from workspace_documents import edit_document
    from workspace_journal import snapshot

    project = _project(tmp_path, 5)
    append_record(project, "decision", "A decision.", entry_id="q1")
    _set(project, "T01", spec_refs=[HEADING], decision_refs=["q1"])
    clean = edit_document(project, "spec", expected_sha256=snapshot(project / "spec.md")[1], sections={OTHER: "x"})
    assert "warnings" not in clean
    (project / "spec.md").write_text(_spec(project).replace(f"### {HEADING}", "### Renamed"), encoding="utf-8")
    edited = edit_document(project, "spec", expected_sha256=snapshot(project / "spec.md")[1], sections={OTHER: "y"})
    assert len(edited["warnings"]) == 1 and f"names no `###` heading under `## Current specification`: {HEADING}" in (
        edited["warnings"][0]
    )
    older = _project(tmp_path / "older", 4)
    plain = edit_document(older, "spec", expected_sha256=snapshot(older / "spec.md")[1], sections={OTHER: "z"})
    assert "warnings" not in plain


# --- re-audit fixes: commit trigger, structured spec-edit filter and a guarded warning ---------------------


def test_only_a_start_counts_as_a_status_touch(tmp_path: Path) -> None:
    from workspace_operations import task_operation

    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=["stale"])  # T01 is RUNNING with a heading that no longer exists
    _set(project, "T02", spec_refs=["stale"])
    task_operation(project, "block", "T01", expected_revision=_load_state(project)["revision"], reason="parked")
    assert _load_state(project)["tasks"][0]["status"] == "BLOCKED"  # a block is not refused
    with pytest.raises(WorkspaceError, match="task T02: spec_refs names no"):
        task_operation(project, "start", "T02", expected_revision=_load_state(project)["revision"])


def test_dangling_only_leaves_out_a_task_with_no_references(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    state = _load_state(project)
    assert any("needs at least one" in e for e in reference_errors(project, state))
    assert reference_errors(project, state, dangling_only=True) == []


def test_a_failing_reference_check_after_a_spec_edit_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workspace_documents import edit_document
    from workspace_journal import snapshot

    project = _project(tmp_path, 5)

    def fail(*args: Any, **kwargs: Any) -> list[str]:
        raise OSError("disk went away")

    monkeypatch.setattr(workspace_packets, "reference_errors", fail)
    edited = edit_document(project, "spec", expected_sha256=snapshot(project / "spec.md")[1], sections={OTHER: "x"})
    assert edited["warnings"] == [
        "a spec edit left references that no longer resolve: the references could not be checked: disk went away"
    ]
