"""Evidence applicability (T05): fingerprint capture, entry parsing, classification and the two reports."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import workspace_context
from workspace_context import (
    Budget,
    capture_fingerprint,
    classify_evidence,
    evidence_applicability,
    fingerprint,
    hash_path,
)
from workspace_evidence import _applicability_lines, _entry_rows
from workspace_lib import WorkspaceError, record_evidence_result
from workspace_operations import task_operation
from workspace_workflows import workflow

from tests.plugins.research.project.test_packets import _load_state, _project, _set

SHA = "ab" * 32


def _target(project: Path) -> Path:
    return Path(_load_state(project)["working_directory"])


def _task(project: Path, task_id: str = "T01") -> dict[str, Any]:
    return next(task for task in _load_state(project)["tasks"] if task["id"] == task_id)


def _declare(project: Path, reads: list[str], outputs: "list[dict[str, Any]] | None" = None) -> None:
    _set(project, "T01", reads=reads, outputs=outputs or [])


def _record(project: Path, task: str = "T01") -> str:
    result = record_evidence_result(project, task, ["true"])
    return result.record_id


def _row(project: Path, record_id: str) -> dict[str, Any]:
    return next(row for row in _entry_rows((project / "evidence.md").read_text(encoding="utf-8"))
                if row["record_id"] == record_id)


def _classify(project: Path, record_id: str, budget: "Budget | None" = None) -> "dict[str, Any] | None":
    return classify_evidence(project, _load_state(project), _row(project, record_id), budget or Budget())


def _git(target: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(target), *arguments], check=True, capture_output=True)


def _repository(target: Path) -> None:
    _git(target, "init", "-q")
    _git(target, "config", "user.email", "t@example.com")
    _git(target, "config", "user.name", "t")


# --- Budget, hash_path and fingerprint() ------------------------------------------------------------


def test_budget_is_shared_and_stays_spent() -> None:
    budget = Budget(10)
    assert budget.take(6) and budget.remaining == 4 and not budget.exhausted
    assert not budget.take(5) and budget.exhausted and budget.remaining == 4
    assert Budget().remaining == 16 * 1024 * 1024


def test_hash_path_kinds(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x", encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a").write_text("1", encoding="utf-8")
    assert hash_path(tmp_path / "none", Budget()) == ("missing", None)
    kind, digest = hash_path(tmp_path / "f", Budget())
    assert kind == "file" and re.fullmatch(r"[0-9a-f]{64}", digest or "")
    kind, first = hash_path(tmp_path / "d", Budget())
    assert kind == "dir"
    (tmp_path / "d" / "a").write_text("2", encoding="utf-8")
    assert hash_path(tmp_path / "d", Budget())[1] != first


def test_directory_hash_skips_links_git_and_empty_directories(tmp_path: Path) -> None:
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "a").write_text("1", encoding="utf-8")
    base = hash_path(folder, Budget())[1]
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("noise", encoding="utf-8")
    (folder / "empty").mkdir()
    (folder / "link").symlink_to(folder / "a")
    (folder / "dlink").symlink_to(tmp_path)
    os.mkfifo(folder / "pipe")
    assert hash_path(folder, Budget())[1] == base
    (folder / "sub").mkdir()
    (folder / "sub" / "b").write_text("2", encoding="utf-8")
    assert hash_path(folder, Budget())[1] != base


def test_hash_path_refuses_what_fingerprint_refuses(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(WorkspaceError, match="budget exceeded or path is not a file"):
        hash_path(tmp_path / "pipe", Budget())
    (tmp_path / "big").write_text("12345", encoding="utf-8")
    budget = Budget(4)
    with pytest.raises(WorkspaceError, match="budget exceeded"):
        hash_path(tmp_path / "big", budget)
    assert budget.exhausted


def test_a_file_that_grows_while_read_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f"
    path.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(Path, "stat", lambda self, **kw: os.stat_result((0,) * 10))
    with pytest.raises(WorkspaceError, match="grew beyond budget"):
        workspace_context._file_digest(path, Budget(2))


def test_fingerprint_keeps_its_contract(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    target = _target(project)
    (target / "f").write_text("x", encoding="utf-8")
    (target / "d").mkdir()
    (first,) = fingerprint(project, [{"root": "target", "path": "f"}])
    assert first["changed"] is None and len(first["sha256"]) == 64
    (missing,) = fingerprint(project, [{"root": "target", "path": "none", "sha256": "x"}])
    assert missing["sha256"] == "missing" and missing["changed"] is True
    with pytest.raises(WorkspaceError, match="not a file"):
        fingerprint(project, [{"root": "target", "path": "d"}])


# --- capture ---------------------------------------------------------------------------------------


def test_capture_writes_one_line_per_declared_path(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    target = _target(project)
    (target / "src").mkdir()
    (target / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (target / "dir").mkdir()
    (target / "dir" / "x").write_text("x", encoding="utf-8")
    _declare(project, ["src/a.py", "src/a.py", "gone.md"], [
        {"root": "target", "path": "dir", "required": True},
        {"root": "workspace", "path": "spec.md", "required": True},
        {"root": "external", "path": "https://example.com/x", "required": False},
    ])
    state = _load_state(project)
    lines = capture_fingerprint(project, state, _task(project))
    assert lines[0] == "- Assignment counter: 1"
    matches = [re.fullmatch(r"- Fingerprint: (\S+) (\S+) (\S+) (.+)", line) for line in lines[1:]]
    kinds = [match.groups() for match in matches if match is not None]
    assert len(kinds) == len(matches)
    assert [(k, r, p) for k, r, _, p in kinds] == [
        ("file", "target", "src/a.py"), ("missing", "target", "gone.md"), ("dir", "target", "dir"),
        ("file", "workspace", "spec.md"),
    ]
    assert kinds[1][2] == "-" and all(len(h) == 64 for k, _, h, _ in kinds if k in ("file", "dir"))


def test_capture_marks_the_unreadable_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, 5)
    target = _target(project)
    outside = tmp_path / "outside"
    outside.write_text("x", encoding="utf-8")
    (target / "escape").symlink_to(outside)
    os.mkfifo(target / "pipe")
    (target / "odd\nname").write_text("x", encoding="utf-8")
    (target / "ok").write_text("x", encoding="utf-8")
    _declare(project, ["escape", "pipe", "odd\nname", "ok"])
    real = workspace_context.hash_path

    def boom(path: Path, budget: Budget) -> tuple[str, "str | None"]:
        if path.name == "ok":
            raise PermissionError("denied")
        return real(path, budget)

    monkeypatch.setattr(workspace_context, "hash_path", boom)
    lines = capture_fingerprint(project, _load_state(project), _task(project))[1:]
    assert lines == [
        "- Fingerprint: unreadable target - escape",
        "- Fingerprint: unreadable target - pipe",
        "- Fingerprint: unreadable target - 'odd\\nname'",
        "- Fingerprint: unreadable target - ok",
    ]


def test_capture_after_a_budget_overrun_marks_every_later_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, 5)
    target = _target(project)
    (target / "big").write_text("12345678", encoding="utf-8")
    (target / "small").write_text("1", encoding="utf-8")
    _declare(project, ["small", "big", "small2", "none"])
    (target / "small2").write_text("1", encoding="utf-8")
    monkeypatch.setattr(workspace_context, "FINGERPRINT_LIMIT", 4)
    lines = capture_fingerprint(project, _load_state(project), _task(project))[1:]
    assert [line.split()[2] for line in lines] == ["file", "over-budget", "over-budget", "over-budget"]
    assert lines[1].endswith("- big")


def test_an_unreadable_subdirectory_makes_the_directory_unreadable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    project = _project(tmp_path, 5)
    target = _target(project)
    (target / "d" / "locked").mkdir(parents=True)
    (target / "d" / "locked" / "f").write_text("x", encoding="utf-8")
    (target / "d" / "locked").chmod(0)
    try:
        _declare(project, ["d"])
        assert capture_fingerprint(project, _load_state(project), _task(project))[1] == (
            "- Fingerprint: unreadable target - d"
        )
    finally:
        (target / "d" / "locked").chmod(0o700)


def test_a_task_with_no_declared_paths_has_the_counter_line_only(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    assert capture_fingerprint(project, _load_state(project), _task(project)) == ["- Assignment counter: 1"]


# --- the entry the recorder writes and the parser reads -------------------------------------------


def test_record_evidence_writes_the_lines_before_the_first_fence(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    (_target(project) / "a.py").write_text("x", encoding="utf-8")
    _declare(project, ["a.py"])
    record_id = _record(project)
    row = _row(project, record_id)
    assert row["selectable"] and row["counter"] == 1
    assert [(f["kind"], f["root"], f["path"]) for f in row["fingerprints"]] == [("file", "target", "a.py")]
    head = row["text"].split("```")[0]
    assert "- Assignment counter: 1\n- Fingerprint: file target " in head


def test_only_task_entries_at_policy_five_carry_the_lines(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    step = record_evidence_result(project, None, ["true"], step="report")
    assert "Fingerprint" not in (project / "evidence.md").read_text(encoding="utf-8")
    assert step.owner_kind == "step"
    older = _project(tmp_path / "older", 4)
    _record(older)
    assert "Assignment counter" not in (older / "evidence.md").read_text(encoding="utf-8")


def test_parsing_tolerates_old_duplicate_and_malformed_lines() -> None:
    good = (
        f"- Assignment counter: 3\n- Fingerprint: file target {SHA} src/a b.py\n"
        "- Fingerprint: missing workspace - x\n"
    )
    counter, prints = _applicability_lines(good)
    assert counter == 3 and [p["kind"] for p in prints] == ["file", "missing"] and prints[0]["path"] == "src/a b.py"
    assert _applicability_lines("") == (None, [])
    assert _applicability_lines(f"- Fingerprint: file target {SHA} a\n") == (None, [])
    assert _applicability_lines("- Assignment counter: 1\n- Assignment counter: 1\n") == (None, [])
    assert _applicability_lines("- Assignment counter: 01\n") == (None, [])
    assert _applicability_lines("- Assignment counter: 1\n- Fingerprint: file target - a\n") == (None, [])
    assert _applicability_lines(f"- Assignment counter: 1\n- Fingerprint: dir target {SHA}\n") == (None, [])
    assert _applicability_lines("- Assignment counter: 1\n- Fingerprint: nope target - a\n") == (None, [])
    assert _applicability_lines("- Assignment counter: 2\r\n") == (2, [])


def test_a_malformed_fingerprint_leaves_the_entry_selectable(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    record_id = _record(project)
    text = (project / "evidence.md").read_text(encoding="utf-8")
    (project / "evidence.md").write_text(
        text.replace("- Assignment counter: 1\n", "- Assignment counter: 1\n- Fingerprint: garbage\n"), encoding="utf-8"
    )
    row = _row(project, record_id)
    assert row["selectable"] and row["counter"] is None and row["fingerprints"] == []


# --- classification -------------------------------------------------------------------------------


def _recorded(tmp_path: Path, reads: list[str], files: "dict[str, str] | None" = None) -> tuple[Path, str]:
    project = _project(tmp_path, 5)
    target = _target(project)
    for name, body in (files or {}).items():
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(body, encoding="utf-8")
    _declare(project, reads)
    return project, _record(project)


def test_unchanged_files_are_applicable(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, ["a.py", "none"], {"a.py": "x"})
    assert _classify(project, record_id) == {
        "record_id": record_id, "task": "T01", "state": "applicable", "reasons": [], "dirty": [],
    }


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda t: (t / "a.py").write_text("y", encoding="utf-8"), "file then, file now"),
        (lambda t: (t / "a.py").unlink(), "file then, missing now"),
        (lambda t: (t / "gone").write_text("new", encoding="utf-8"), "missing then, file now"),
        (lambda t: (t / "d" / "x").write_text("2", encoding="utf-8"), "dir then, dir now"),
    ],
)
def test_a_changed_path_invalidates(tmp_path: Path, change: Any, reason: str) -> None:
    project, record_id = _recorded(tmp_path, ["a.py", "gone", "d"], {"a.py": "x", "d/x": "1"})
    change(_target(project))
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "invalidated" and result["dirty"] == []
    assert any(reason in item for item in result["reasons"])


def test_a_moved_counter_invalidates_before_any_file_is_read(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, ["a.py"], {"a.py": "x"})
    _set(project, "T01", assignment_counter=2)
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "invalidated"
    assert result["reasons"] == ["assignment counter moved from 1 to 2"]


def test_an_entry_without_a_fingerprint_needs_judgement(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, [])
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "needs-judgement"
    assert result["reasons"] == ["the task declared no local path to fingerprint"]
    text = (project / "evidence.md").read_text(encoding="utf-8").replace("- Assignment counter: 1\n", "")
    (project / "evidence.md").write_text(text, encoding="utf-8")
    again = _classify(project, record_id)
    assert again is not None and again["state"] == "needs-judgement"
    assert again["reasons"] == ["no fingerprint was recorded with this entry"]


def test_a_failed_run_is_never_applicable(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    (_target(project) / "a.py").write_text("x", encoding="utf-8")
    _declare(project, ["a.py"])
    failed = record_evidence_result(project, "T01", ["false"])
    assert failed.exit_code == 1
    result = _classify(project, failed.record_id)
    assert result is not None and result["state"] == "needs-judgement"
    assert result["reasons"] == ["the recorded run did not pass"]


def test_a_path_with_edge_whitespace_is_recorded_as_unreadable_and_keeps_the_other_lines(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    (_target(project) / "a.py").write_text("x", encoding="utf-8")
    _declare(project, [" a.py", "a.py"])
    record_id = _record(project)
    row = _row(project, record_id)
    assert [(f["kind"], f["path"]) for f in row["fingerprints"]] == [("unreadable", "' a.py'"), ("file", "a.py")]


def test_a_directory_of_many_files_runs_out_of_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, 5)
    folder = _target(project) / "d"
    folder.mkdir()
    for name in "abc":
        (folder / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(workspace_context, "ENTRY_COST", workspace_context.FINGERPRINT_LIMIT // 2)
    _declare(project, ["d"])
    assert capture_fingerprint(project, _load_state(project), _task(project))[1] == (
        "- Fingerprint: over-budget target - d"
    )


def test_unreadable_and_over_budget_paths_need_judgement_and_changes_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, record_id = _recorded(tmp_path, ["a.py", "b.py"], {"a.py": "x", "b.py": "y"})
    budget = Budget(0)
    result = _classify(project, record_id, budget)
    assert result is not None and result["state"] == "needs-judgement"
    assert result["reasons"] == ["target:a.py: over-budget now", "target:b.py: over-budget now"]
    # a recorded unreadable or over-budget path stays uncertain whatever it is now
    text = (project / "evidence.md").read_text(encoding="utf-8")
    forged = re.sub(r"- Fingerprint: file target [0-9a-f]{64} a\.py", "- Fingerprint: unreadable target - a.py", text)
    (project / "evidence.md").write_text(forged, encoding="utf-8")
    result = _classify(project, record_id)
    assert result is not None and result["reasons"] == ["target:a.py: recorded as unreadable"]
    (_target(project) / "b.py").write_text("changed", encoding="utf-8")
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "invalidated"  # a change wins over uncertainty


def test_closure_step_and_observation_entries_are_not_classified(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    step = record_evidence_result(project, None, ["true"], step="report").record_id
    assert _classify(project, step) is None
    from workspace_lib import record_observation

    observed = record_observation(project, "T01", "looked", source="a page", result="passed").record_id
    assert _classify(project, observed) is None


def test_dirty_paths_are_listed_for_an_applicable_result(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    target = _target(project)
    _repository(target)
    (target / "a.py").write_text("x", encoding="utf-8")
    (target / "d").mkdir()
    (target / "d" / "one").write_text("1", encoding="utf-8")
    (target / "d" / "two").write_text("2", encoding="utf-8")
    (target / "clean.py").write_text("c", encoding="utf-8")
    _git(target, "add", "clean.py")
    _git(target, "commit", "-q", "-m", "x")
    _declare(project, ["a.py", "d", "clean.py"])
    record_id = _record(project)
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "applicable"
    assert sorted(result["dirty"]) == ["a.py", "d/"]  # untracked, hash unchanged; a directory is one entry


def test_a_non_git_target_or_a_failing_git_leaves_the_hash_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, record_id = _recorded(tmp_path, ["a.py"], {"a.py": "x"})
    plain = _classify(project, record_id)  # the target is not a repository
    assert plain is not None and plain["dirty"] == []
    for failure in (subprocess.TimeoutExpired("git", 5), FileNotFoundError("git")):
        def fail(*args: Any, error: Exception = failure, **kwargs: Any) -> str:
            raise error

        monkeypatch.setattr(workspace_context, "_git", fail)
        result = _classify(project, record_id)
        assert result is not None and result["state"] == "applicable" and result["dirty"] == []


def test_only_workspace_paths_skip_the_git_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, 5)
    _declare(project, [], [{"root": "workspace", "path": "spec.md", "required": True}])
    record_id = _record(project)

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("git must not be asked about workspace paths")

    monkeypatch.setattr(workspace_context, "_git", refuse)
    result = _classify(project, record_id)
    assert result is not None and result["state"] == "applicable"


# --- the two reports ------------------------------------------------------------------------------


def test_evidence_applicability_names_unknown_records_and_survives_a_missing_file(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, ["a.py"], {"a.py": "x"})
    assert evidence_applicability(project, _load_state(project), []) == []
    unknown = "ev-" + "0" * 32
    report = evidence_applicability(project, _load_state(project), [unknown, record_id])
    assert [(item["record_id"], item["state"]) for item in report] == [
        (unknown, "needs-judgement"), (record_id, "applicable"),
    ]
    assert report[0]["reasons"] == ["the record is not in evidence.md"]
    (project / "evidence.md").unlink()
    (failure,) = evidence_applicability(project, _load_state(project), [record_id])
    assert failure["state"] == "needs-judgement" and failure["reasons"][0].startswith("the applicability report failed")


def test_cited_record_ids_skip_references_that_name_no_evidence_record(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, ["a.py"], {"a.py": "x"})
    anchor = "evidence-" + record_id[3:]
    cited = {"root": "workspace", "path": "evidence.md", "anchor": anchor}
    state = _load_state(project)
    assert workspace_context.cited_record_ids(state) == []  # a running task that cites nothing
    for evidence in (
        [{"root": "workspace", "path": "evidence.md", "anchor": None}],
        [{"root": "external", "path": "https://example.com/x", "anchor": anchor}],
        [{"root": "workspace", "path": "other.md", "anchor": anchor}],
        [{"root": "workspace", "path": "evidence.md", "anchor": "evidence-short"}],
    ):
        _set(project, "T01", evidence=evidence)
        assert workspace_context.cited_record_ids(_load_state(project)) == []
        assert workflow(project, "resume", {})["evidence_applicability"] == []  # resume does not crash
    _set(project, "T01", evidence=[cited, cited])
    assert workspace_context.cited_record_ids(_load_state(project)) == [record_id]


def _local_task_with_evidence(tmp_path: Path, policy: int) -> tuple[Path, str]:
    """T02 (a local task) running with a declared read and one recorded check; the file is `a.py`."""
    project = _project(tmp_path, policy)
    (_target(project) / "a.py").write_text("x", encoding="utf-8")
    _set(project, "T02", reads=["a.py"], **({"spec_refs": ["Objective and audience"]} if policy == 5 else {}))
    task_operation(project, "start", "T02", expected_revision=_load_state(project)["revision"])
    return project, _record(project, "T02")


def test_task_finish_reports_applicability_without_blocking(tmp_path: Path) -> None:
    from workspace_operations import finish_and_checkpoint

    project, record_id = _local_task_with_evidence(tmp_path, 5)
    (_target(project) / "a.py").write_text("changed after the check", encoding="utf-8")
    result = finish_and_checkpoint(
        project, "T02", expected_revision=_load_state(project)["revision"], evidence_record_ids=[record_id]
    )
    (item,) = result["evidence_applicability"]
    assert item["state"] == "invalidated" and item["task"] == "T02" and _task(project, "T02")["status"] == "DONE"
    with pytest.MonkeyPatch.context() as patch:  # a failing report is data, never a failed finish
        broken, broken_record = _local_task_with_evidence(tmp_path / "broken", 5)

        def explode(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise WorkspaceError("evidence.md cannot be read")

        patch.setattr(workspace_context, "evidence_applicability", explode)
        done = finish_and_checkpoint(
            broken, "T02", expected_revision=_load_state(broken)["revision"], evidence_record_ids=[broken_record]
        )
    assert _task(broken, "T02")["status"] == "DONE"
    (failure,) = done["evidence_applicability"]
    assert failure["reasons"] == ["the applicability report failed: evidence.md cannot be read"]
    older, old_record = _local_task_with_evidence(tmp_path / "older", 4)
    plain = finish_and_checkpoint(
        older, "T02", expected_revision=_load_state(older)["revision"], evidence_record_ids=[old_record]
    )
    assert "evidence_applicability" not in plain


def test_resume_reports_the_latest_cited_record_of_each_running_task(tmp_path: Path) -> None:
    project, record_id = _recorded(tmp_path, ["a.py"], {"a.py": "x"})
    assert workflow(project, "resume", {})["evidence_applicability"] == []
    reference = {"root": "workspace", "path": "evidence.md", "anchor": "evidence-" + record_id[3:]}
    _set(project, "T01", evidence=[reference])
    (_target(project) / "a.py").write_text("changed", encoding="utf-8")
    (item,) = workflow(project, "resume", {})["evidence_applicability"]
    assert item["record_id"] == record_id and item["state"] == "invalidated"
    older = _project(tmp_path / "older", 4)
    assert "evidence_applicability" not in workflow(older, "resume", {})
    packet = workflow(project, "packet", {"task": "T01", "worker": "w", "scope": "s", "lessons": []})
    assert "evidence_applicability" not in packet
