"""One rule, every entry point: `task finish` and a revision-checked `update` must agree.

A command exiting 7 was refused by `task finish` and accepted by an `update` setting the same task
DONE with the same record. The rule now lives in the commit path both share, so each case below runs
through both entry points and expects the same verdict. Projects are made "current" by moving the
gate cutoff into the past after the project reaches PLANNING, and "legacy" by leaving it in 2099.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import workspace_evidence as evidence
import workspace_lib as lib
import workspace_operations as operations
from workspace_lib import WorkspaceError
from workspace_session import update_project_data

from tests.plugins.research.project.test_workflow_automation import invoke, plan

PAST_CUTOFF = "2020-01-01T00:00:00+00:00"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Equivalence", working_directory=target, create_root=True
    )
    plan(created)
    return created


@pytest.fixture
def current(project: Path, current_mode: str) -> Path:
    """A project the gates apply to: created after the cutoff, which is moved into the past."""
    assert lib.is_new_project(json.loads((project / "project.json").read_text(encoding="utf-8")))
    return project


@pytest.fixture(params=["legacy", "current"])
def mode(request: pytest.FixtureRequest, project: Path) -> str:
    if request.param == "current":
        request.getfixturevalue("current_mode")
    return str(request.param)


def revision(project: Path) -> int:
    return int(json.loads((project / "project.json").read_text(encoding="utf-8"))["revision"])


def state(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def start(project: Path, task: str = "T01") -> None:
    operations.task_operation(project, "start", task, expected_revision=revision(project))


def command(project: Path, task: str = "T01", code: int = 0) -> str:
    return lib.record_evidence_result(project, task, [sys.executable, "-c", f"raise SystemExit({code})"]).record_id


def observe(project: Path, task: str = "T01", result: str = "passed") -> str:
    return lib.record_observation(project, task, "42 rows", source="a read", result=result).record_id  # type: ignore[attr-defined]


def anchor(record_id: str) -> dict[str, Any]:
    return {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{record_id[3:]}"}


def finish_via(
    entry: str, project: Path, *, commands: Sequence[str] = (), observations: Sequence[str] = (), task: str = "T01"
) -> Any:
    if entry == "finish":
        return operations.task_operation(
            project, "finish", task, expected_revision=revision(project),
            evidence_record_ids=list(commands), observation_record_ids=list(observations),
        )
    references = [anchor(record) for record in [*commands, *observations]]
    patch = {"tasks": [{"id": task, "status": "DONE", "evidence": references}]}
    return update_project_data(project, patch, expected_revision=revision(project))


def update_with(project: Path, references: list[dict[str, Any]], task: str = "T01") -> Any:
    patch = {"tasks": [{"id": task, "status": "DONE", "evidence": references}]}
    return update_project_data(project, patch, expected_revision=revision(project))


ENTRIES = ["finish", "update"]


class TestEveryEntryPointRefusesTheSameBadRecords:
    @pytest.mark.parametrize("entry", ENTRIES)
    def test_a_failed_command_record_cannot_finish_a_task(self, project: Path, mode: str, entry: str) -> None:
        start(project)
        failed = command(project, code=7)
        before = revision(project)
        with pytest.raises(WorkspaceError, match=failed):
            finish_via(entry, project, commands=[failed])
        assert revision(project) == before and state(project)["tasks"][0]["status"] == "RUNNING"

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_a_failed_observation_cannot_finish_a_task(self, project: Path, mode: str, entry: str) -> None:
        start(project)
        failed = observe(project, result="FAILED")
        with pytest.raises(WorkspaceError, match=failed):
            finish_via(entry, project, observations=[failed])

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_another_tasks_record_cannot_finish_a_task(self, project: Path, mode: str, entry: str) -> None:
        start(project)
        foreign = command(project, task="T02")
        with pytest.raises(WorkspaceError, match=foreign):
            finish_via(entry, project, commands=[foreign])

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_a_passing_command_record_finishes_a_task(self, project: Path, mode: str, entry: str) -> None:
        start(project)
        finish_via(entry, project, commands=[command(project)])
        assert state(project)["tasks"][0]["status"] == "DONE"

    def test_the_cli_update_refuses_what_the_python_update_refuses(self, current: Path) -> None:
        start(current)
        failed = command(current, code=7)
        patch = {"tasks": [{"id": "T01", "status": "DONE", "evidence": [anchor(failed)]}]}
        code, out, err = invoke(
            ["update", str(current), "-", "--expected-revision", str(revision(current)), "--json"],
            stdin=json.dumps(patch),
        )
        assert code != 0 and failed in (out + err)
        assert state(current)["tasks"][0]["status"] == "RUNNING"


class TestObservationOnlyFinish:
    def test_current_projects_finish_on_observations_only_through_the_attesting_command(self, current: Path) -> None:
        start(current)
        record = observe(current)
        result = finish_via("finish", current, observations=[record])
        assert result["attested"] is True and state(current)["tasks"][0]["status"] == "DONE"

    def test_current_projects_cannot_skip_the_attestation_through_update(self, current: Path) -> None:
        start(current)
        with pytest.raises(WorkspaceError, match="--observation"):
            finish_via("update", current, observations=[observe(current)])
        assert state(current)["tasks"][0]["status"] == "RUNNING"

    def test_an_unsafe_task_id_never_counts_as_attested(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", PAST_CUTOFF)
        (tmp_path / "evidence.md").write_text("# Evidence\n", encoding="utf-8")
        candidate = {"created": "2030-01-01T00:00:00+00:00", "tasks": [
            {"id": "T 1", "status": "DONE", "evidence": []},
        ]}
        errors, _ = _judged(tmp_path, candidate)
        assert errors == ["task T 1: finishing requires at least one passing evidence record from this task"]

    def test_a_note_without_the_attested_entry_does_not_count(self, current: Path) -> None:
        start(current)
        record = observe(current)
        (current / "tasks").mkdir(exist_ok=True)
        (current / "tasks" / "T01.md").write_text("# T01\n\nSome other finding.\n", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="--observation"):
            finish_via("update", current, observations=[record])

    def test_legacy_projects_keep_accepting_observation_only_updates(self, project: Path) -> None:
        start(project)
        finish_via("update", project, observations=[observe(project)])
        assert state(project)["tasks"][0]["status"] == "DONE"


class TestReferencesTheToolCannotJudge:
    def test_a_legacy_anchor_is_an_error_for_current_projects(self, current: Path) -> None:
        start(current)
        before = revision(current)
        with pytest.raises(WorkspaceError, match="not a recorded evidence entry"):
            update_with(current, [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}])
        assert revision(current) == before

    def test_a_legacy_anchor_is_a_warning_for_legacy_projects(self, project: Path) -> None:
        start(project)
        patch = {"tasks": [{"id": "T01", "status": "DONE", "evidence": [
            {"root": "workspace", "path": "evidence.md", "anchor": None}]}]}
        code, out, _ = invoke(
            ["update", str(project), "-", "--expected-revision", str(revision(project)), "--dry-run", "--json"],
            stdin=json.dumps(patch),
        )
        assert code == 0
        assert any("not a recorded evidence entry" in warning for warning in json.loads(out)["warnings"])
        update_with(project, [{"root": "workspace", "path": "evidence.md", "anchor": None}])
        assert state(project)["tasks"][0]["status"] == "DONE"

    def test_an_anchor_matching_no_entry_follows_the_same_split(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start(project)
        missing = [{"root": "workspace", "path": "evidence.md", "anchor": "evidence-" + "0" * 32}]
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", PAST_CUTOFF)
        with pytest.raises(WorkspaceError, match="matches no single well-formed"):
            update_with(project, missing)
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2099-01-01T00:00:00+00:00")
        update_with(project, missing)

    def test_a_malformed_entry_is_not_judged_as_a_pass(self, current: Path) -> None:
        start(current)
        record = command(current)
        text = (current / "evidence.md").read_text(encoding="utf-8")
        (current / "evidence.md").write_text(text.replace("(passed)", "(FAILED)"), encoding="utf-8")
        with pytest.raises(WorkspaceError, match="well-formed"):
            update_with(current, [anchor(record)])

    def test_current_projects_need_a_passing_record_not_only_other_references(self, current: Path) -> None:
        start(current)
        with pytest.raises(WorkspaceError, match="at least one passing evidence record"):
            update_with(current, [{"root": "external", "path": "https://example.com/result", "anchor": None}])


class TestTerminalHistoryIsNotRejudged:
    def test_a_task_already_done_with_a_legacy_anchor_survives_later_commits(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start(project)
        update_with(project, [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}])
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", PAST_CUTOFF)
        start(project, "T02")
        finish_via("finish", project, commands=[command(project, "T02")], task="T02")
        assert [task["status"] for task in state(project)["tasks"]] == ["DONE", "DONE"]


def _judged(project_dir: Path, candidate: dict[str, Any]) -> tuple[list[str], list[str]]:
    return evidence.newly_terminal_task_errors(project_dir, {}, candidate)


class TestMalformedInputsAreSkippedNotRaised:
    @pytest.mark.parametrize(
        "candidate",
        [
            {"tasks": 5},
            {"tasks": ["x", {"id": "T01", "status": "TODO"}, {"status": "DONE"}]},
            {"tasks": [{"id": "T01", "status": "DONE", "evidence": "x"}]},
            {"tasks": [{"id": "T01", "status": "DONE", "evidence": [5, {"root": "target", "path": "a"},
                                                                     {"root": "workspace", "path": "other.md"}]}]},
        ],
    )
    def test_legacy_shapes_yield_no_findings(self, tmp_path: Path, candidate: dict[str, Any]) -> None:
        assert _judged(tmp_path, candidate) == ([], [])

    def test_a_missing_evidence_file_is_an_empty_set_of_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", PAST_CUTOFF)
        candidate = {"created": "2030-01-01T00:00:00+00:00", "tasks": [{"id": "T01", "status": "DONE", "evidence": [
            {"root": "workspace", "path": "evidence.md", "anchor": "evidence-" + "1" * 32}]}]}
        errors, _ = _judged(tmp_path, candidate)
        assert len(errors) == 1 and "matches no single" in errors[0]
