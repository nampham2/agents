"""Tasks of a new project record when they started and finished; older projects stay as they were.

Task spans used to be derived from evidence stamps alone, which made a task with one recorded
command look instantaneous. The stamps are written only for v4 projects created on or after the
gate cutoff, because an older launcher rejects a task that carries them: a project already in
flight must never gain one. New names are looked up when a test runs, not at import.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_operations import task_operation
from workspace_session import update_project_data

from tests.plugins.research.project.test_workflow_automation import fill_spec, task


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))


def task_of(project: Path, task_id: str) -> dict[str, Any]:
    return next(t for t in state_of(project)["tasks"] if t["id"] == task_id)


def evidence_for(project: Path, task_id: str) -> str:
    return lib.record_evidence_result(project, task_id, [sys.executable, "-c", "pass"]).record_id


@pytest.fixture
def planned(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    project = lib.allocate_project(tmp_path / "workspace", title="Stamps", working_directory=target, create_root=True)
    fill_spec(project)
    update_project_data(
        project, {"status": "PLANNING", "tasks": [task("T01"), task("T02", depends_on=["T01"]), task("T03")]},
        expected_revision=0,
    )
    return project


@pytest.fixture
def new_project(planned: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Legacy by default (the autouse fixture); the cutoff is moved into the past for this one.
    monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
    assert lib.task_stamps_enabled(state_of(planned))
    return planned


class TestStamping:
    def test_start_and_finish_stamp_a_new_projects_task(self, new_project: Path) -> None:
        task_operation(new_project, "start", "T01", expected_revision=1)
        started = task_of(new_project, "T01")
        assert lib._is_timestamp(started["started_at"]) and "finished_at" not in started
        record = evidence_for(new_project, "T01")
        task_operation(new_project, "finish", "T01", expected_revision=2, evidence_record_ids=[record])
        finished = task_of(new_project, "T01")
        assert finished["started_at"] == started["started_at"]
        assert lib._is_timestamp(finished["finished_at"]) and finished["finished_at"] >= finished["started_at"]
        assert lib.validate_project(new_project).valid

    def test_a_restart_after_blocked_keeps_the_first_start(self, new_project: Path) -> None:
        task_operation(new_project, "start", "T01", expected_revision=1)
        first = task_of(new_project, "T01")["started_at"]
        task_operation(new_project, "block", "T01", expected_revision=2, reason="waiting")
        task_operation(new_project, "start", "T01", expected_revision=3)
        assert task_of(new_project, "T01")["started_at"] == first

    def test_start_next_stamps_the_successor(self, new_project: Path) -> None:
        task_operation(new_project, "start", "T01", expected_revision=1)
        record = evidence_for(new_project, "T01")
        task_operation(
            new_project, "finish", "T01", expected_revision=2, evidence_record_ids=[record], start_next="T02"
        )
        assert lib._is_timestamp(task_of(new_project, "T02")["started_at"])
        assert "started_at" not in task_of(new_project, "T03")

    def test_a_backfilled_task_gets_finished_at_only(self, new_project: Path) -> None:
        record = evidence_for(new_project, "T03")
        result = task_operation(
            new_project, "finish", "T03", expected_revision=1, evidence_record_ids=[record],
            backfill=True, note="done while drafting",
        )
        assert result["backfilled"] is True
        finished = task_of(new_project, "T03")
        assert "started_at" not in finished and lib._is_timestamp(finished["finished_at"])
        assert lib.validate_project(new_project).valid

    def test_skip_and_block_write_no_stamps(self, new_project: Path) -> None:
        task_operation(new_project, "block", "T01", expected_revision=1, reason="waiting")
        task_operation(new_project, "skip", "T03", expected_revision=2, reason="not needed")
        for task_id in ("T01", "T03"):
            assert not {"started_at", "finished_at"} & task_of(new_project, task_id).keys()


class TestWhoIsStamped:
    def test_a_legacy_project_is_never_stamped(self, planned: Path) -> None:
        assert not lib.task_stamps_enabled(state_of(planned))
        task_operation(planned, "start", "T01", expected_revision=1)
        record = evidence_for(planned, "T01")
        task_operation(planned, "finish", "T01", expected_revision=2, evidence_record_ids=[record], start_next="T02")
        for task_id in ("T01", "T02"):
            assert not {"started_at", "finished_at"} & task_of(planned, task_id).keys()

    def test_a_v3_project_is_never_stamped_even_when_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        state = {"schema_version": 3, "created": "2026-10-02T10:00:00+02:00"}
        assert lib.is_new_project(state) and not lib.task_stamps_enabled(state)

    def test_the_previous_release_refuses_a_stamped_project_with_the_upgrade_hint(
        self, new_project: Path, tmp_path: Path
    ) -> None:
        baseline = Path("/tmp/research-baseline-0.20.0/plugins/research/skills/project/scripts/research-validate")
        if not baseline.exists():  # pragma: no cover - the export is made by the project's baseline task
            pytest.skip("no 0.20.0 export at /tmp/research-baseline-0.20.0")
        task_operation(new_project, "start", "T01", expected_revision=1)
        result = subprocess.run([str(baseline), str(new_project)], capture_output=True, text=True)
        assert result.returncode == 1
        assert "unexpected fields: started_at" in result.stderr
        assert "a newer plugin may have written this" in result.stderr
