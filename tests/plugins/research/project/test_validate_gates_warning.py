"""`research-validate` warns when a DONE code task of a gated project has no sound observability record.

The projects are hand-built: the record and its finding are written the way `gate-run` writes them, so the
validator is tested alone. Every case that must stay quiet carries a twin that must warn, so a test cannot pass
because the fixture is empty.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
import workspace_observe as observe
from validate_workspace import main as validate_main
from workspace_documents import append_record
from workspace_lib import validate_project

from tests.plugins.research.project.test_workspace_gates import (
    APPROVAL,
    BASE,
    LINT,
    gates_object,
    make_project,
    make_repo,
    raw_state,
    write_state,
)

TREE = "c" * 64
WAIVER = {"reason": "no repo", "source": "user", "response": "ok", "granted_at": "2026-11-23T11:00:00+00:00"}
ENABLED = {"base_commit": BASE, "sources": [], "commands": [LINT], "approval": APPROVAL}
HEADER = "gated project has a DONE code task"


def record_for(task: str, run: str, files: "list[dict[str, Any]] | None" = None) -> dict[str, Any]:
    return observe.build_record(
        task=task,
        base_commit=BASE,
        head_tree=TREE,
        run=run,
        commands_sha256="d" * 64,
        files=files if files is not None else [
            {"path": "app.py", "added": 3, "removed": 0, "language": "python", "kind": "source"}
        ],
        tests={"files": 1, "cases": 2},
        covered=3,
        total=3,
        complexity={"touched": 1, "max": None, "over_limit": []},
        scan={"findings": [], "info": []},
        failures=[],
    )


def write_record(
    project: Path, task: str = "T01", *, files: "list[dict[str, Any]] | None" = None, number: int = 1
) -> Path:
    """The record and finding of run `number`; a later run's finding supersedes an earlier one."""
    run = f"gates-{task.lower()}-{number}"
    record = record_for(task, run, files)
    path = project / "observability" / f"{task}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    append_record(
        project,
        "finding",
        f"head_tree={TREE} run={run} record_sha256={digest}",
        task_id=task,
        entry_id=run,
    )
    return path


def finish(project: Path, *tasks: str, finished_at: "str | None" = None, gates: "dict[str, Any] | None" = None) -> None:
    state = raw_state(project)
    for task in state["tasks"]:
        if task["id"] in tasks:
            task["status"] = "DONE"
            task["evidence"] = [{"root": "workspace", "path": "evidence.md", "anchor": None}]
            if finished_at is not None:
                task["finished_at"] = finished_at
    if gates is not None:
        state["gates"] = gates
    write_state(project, state)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A gated project: T01 is a DONE code task, T02 a DONE prose task; no record exists yet."""
    created = make_project(tmp_path, make_repo(tmp_path), gates_field=gates_object(**ENABLED))
    finish(created, "T01", "T02")
    return created


def gate_warnings(project: Path) -> list[str]:
    report = validate_project(project)
    return [item for item in report.warnings if "observability" in item or "unsupported language" in item]


class TestMissingRecord:
    def test_a_done_code_task_without_a_record_warns_with_the_task_and_the_fix(self, project: Path) -> None:
        warnings = gate_warnings(project)
        assert len(warnings) == 1, warnings
        assert "T01" in warnings[0] and HEADER in warnings[0] and "no valid observability record" in warnings[0]
        assert "start the task, run gate-run, then finish" in warnings[0]

    def test_a_valid_record_silences_it(self, project: Path) -> None:
        assert gate_warnings(project)  # the control: the same project warned without the record
        write_record(project)
        assert gate_warnings(project) == []


class TestInvalidRecord:
    def test_a_tampered_record_warns(self, project: Path) -> None:
        path = write_record(project)
        assert gate_warnings(project) == []
        path.write_text(path.read_text(encoding="utf-8").replace('"cases": 2', '"cases": 99'), encoding="utf-8")
        warnings = gate_warnings(project)
        assert len(warnings) == 1 and "T01" in warnings[0] and "changed since" in warnings[0]

    def test_a_malformed_record_warns_with_the_first_finding(self, project: Path) -> None:
        path = write_record(project)
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["tests"]
        path.write_text(json.dumps(record), encoding="utf-8")
        _refresh_finding(project, path)
        warnings = gate_warnings(project)
        assert len(warnings) == 1 and "malformed" in warnings[0] and "tests" in warnings[0]

    def test_a_record_that_is_not_json_warns(self, project: Path) -> None:
        path = write_record(project)
        path.write_text("not json", encoding="utf-8")
        _refresh_finding(project, path)
        assert "malformed" in gate_warnings(project)[0]

    def test_a_record_of_another_run_or_task_warns(self, project: Path) -> None:
        path = write_record(project)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["task"] = "T09"
        path.write_text(json.dumps(record), encoding="utf-8")
        _refresh_finding(project, path)
        assert "belongs to" in gate_warnings(project)[0]

    def test_a_finding_with_no_record_file_or_no_readable_line_warns(self, project: Path) -> None:
        path = write_record(project)
        path.unlink()
        assert "is missing" in gate_warnings(project)[0]
        append_record(project, "finding", "no hashes here", task_id="T01", entry_id="gates-t01-2")
        assert "cannot be read" in gate_warnings(project)[0]


def _refresh_finding(project: Path, path: Path) -> None:
    """Re-write the finding so the hash matches the edited record: only the record's content is then wrong."""
    notes = project / "tasks" / "T01.md"
    text = notes.read_text(encoding="utf-8")
    old = text[text.index("record_sha256=") + len("record_sha256=") :][:64]
    notes.write_text(text.replace(old, hashlib.sha256(path.read_bytes()).hexdigest()), encoding="utf-8")


class TestWhoIsJudged:
    def test_an_ungated_project_never_warns(self, project: Path) -> None:
        assert gate_warnings(project)
        state = raw_state(project)
        del state["gates"]
        write_state(project, state)
        assert gate_warnings(project) == []

    def test_a_disabled_gate_profile_never_warns(self, project: Path) -> None:
        assert gate_warnings(project)
        state = raw_state(project)
        state["gates"]["enabled"] = False
        write_state(project, state)
        assert gate_warnings(project) == []

    def test_a_done_prose_task_never_warns(self, project: Path) -> None:
        warnings = gate_warnings(project)
        assert [item for item in warnings if "T01" in item] and not [item for item in warnings if "T02" in item]

    def test_a_task_that_is_not_done_never_warns(self, tmp_path: Path) -> None:
        created = make_project(tmp_path, make_repo(tmp_path), gates_field=gates_object(**ENABLED))
        assert gate_warnings(created) == []
        finish(created, "T01")
        assert gate_warnings(created)

    def test_a_no_repository_waiver_needs_no_record(self, project: Path) -> None:
        state = raw_state(project)
        state["gates"]["waiver"] = {**WAIVER, "kinds": ["tests"]}
        write_state(project, state)
        assert gate_warnings(project), "a waiver of another kind does not excuse the record"
        state["gates"]["waiver"] = {**WAIVER, "kinds": ["no-repository"]}
        write_state(project, state)
        assert gate_warnings(project) == []

    def test_a_task_finished_before_the_commands_were_approved_needs_no_record(self, tmp_path: Path) -> None:
        created = make_project(tmp_path, make_repo(tmp_path), gates_field=gates_object(**ENABLED))
        finish(created, "T01", finished_at="2026-11-23T09:00:00+00:00")  # approval is 10:00
        assert gate_warnings(created) == []
        finish(created, "T01", finished_at="2026-11-23T10:30:00+00:00")
        assert len(gate_warnings(created)) == 1
        finish(created, "T01", finished_at="2026-11-23T09:00:00+00:00", gates=gates_object())
        assert len(gate_warnings(created)) == 1, "with no approval there is nothing to have finished before"


class TestUnsupportedLanguage:
    JS: ClassVar[list[dict[str, Any]]] = [
        {"path": "web/app.js", "added": 5, "removed": 0, "language": "other", "kind": "source"}
    ]

    def test_a_non_python_record_is_reported_once_for_the_project(self, tmp_path: Path) -> None:
        created = make_project(tmp_path, make_repo(tmp_path), gates_field=gates_object(**ENABLED))
        finish(created, "T01", "T02")
        state = raw_state(created)
        state["tasks"][1]["outputs"] = [{"root": "target", "path": "web/b.js", "required": True}]
        write_state(created, state)
        files = [*self.JS, {"path": "web/b.js", "added": 1, "removed": 0, "language": "other", "kind": "other"}]
        write_record(created, "T01", files=files)
        write_record(created, "T02", files=files)
        warnings = gate_warnings(created)
        assert len(warnings) == 1, warnings
        assert "unsupported language" in warnings[0] and "T01" in warnings[0] and "T02" in warnings[0]
        assert "web/app.js" not in warnings[0]

    def test_a_python_record_is_not_reported(self, project: Path) -> None:
        write_record(project)
        assert gate_warnings(project) == []
        write_record(project, files=self.JS, number=2)
        assert len(gate_warnings(project)) == 1

    def test_a_mixed_record_with_a_python_file_is_supported(self, project: Path) -> None:
        python = {"path": "app.py", "added": 1, "removed": 0, "language": "python", "kind": "source"}
        write_record(project, files=self.JS)
        assert len(gate_warnings(project)) == 1
        write_record(project, files=[*self.JS, python], number=2)
        assert gate_warnings(project) == []

    def test_a_record_of_docs_and_config_only_is_not_unsupported(self, project: Path) -> None:
        docs = [{"path": "README.md", "added": 1, "removed": 0, "language": "markdown", "kind": "doc"}]
        write_record(project, files=self.JS)
        assert len(gate_warnings(project)) == 1
        write_record(project, files=docs, number=2)
        assert gate_warnings(project) == []

    def test_a_missing_record_is_not_called_unsupported(self, project: Path) -> None:
        assert not [item for item in gate_warnings(project) if "unsupported language" in item]


class TestExitCode:
    def test_the_warnings_leave_the_error_list_and_exit_code_alone(
        self, project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import sys

        report = validate_project(project)
        assert gate_warnings(project) and not [item for item in report.errors if "observability" in item]
        clean = copy.deepcopy(report.errors)
        write_record(project)
        assert validate_project(project).errors == clean
        argv = sys.argv
        sys.argv = ["research-validate", str(project)]
        try:
            code = validate_main()
        finally:
            sys.argv = argv
        assert code == (1 if clean else 0), capsys.readouterr().err


class TestMalformedState:
    def test_a_state_with_findings_gets_no_gate_warning_and_no_exception(self, project: Path) -> None:
        assert gate_warnings(project)  # the control: the same project warned while its state was sound
        state = raw_state(project)
        state["gates"]["waiver"] = {**WAIVER, "kinds": 5}
        write_state(project, state)
        report = validate_project(project)
        assert any("gates.waiver" in item for item in report.errors), report.errors
        assert gate_warnings(project) == []
