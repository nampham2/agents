"""An observation is the agent's own account, labelled as such, and finished on only by name.

Some checks cannot be wrapped in `record-evidence`: an MCP read or a query has no command and no exit
code. Two of 85 historical projects recorded zero commands for that reason. The risk is the very one
the recorder exists to close, a verdict the agent typed, so these tests pin that an observation can
never be mistaken for command evidence, and that an observation-only finish leaves a durable note.

New names are looked up when a test runs, not when the module is imported, so a build without the
feature fails each test on its own instead of failing to import and hiding every other test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_evidence as evidence
import workspace_lib as lib
import workspace_operations as operations
from workspace_lib import WorkspaceError

from tests.plugins.research.project.test_workflow_automation import invoke, plan


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Observations", working_directory=target, create_root=True
    )
    plan(created)
    return created


def observe(project: Path, task: str = "T01", result: str = "passed", body: str = "42 rows", **kw: Any) -> str:
    return lib.record_observation(  # type: ignore[attr-defined]
        project, task, body, source=kw.pop("source", "bq query, table x"), result=result, **kw
    ).record_id


def command(project: Path, task: str = "T01", code: int = 0) -> str:
    return lib.record_evidence_result(
        project, task, [sys.executable, "-c", f"raise SystemExit({code})"]
    ).record_id


def start(project: Path, task: str = "T01", revision: int = 1) -> None:
    operations.task_operation(project, "start", task, expected_revision=revision)


def revision(project: Path) -> int:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))["revision"]


def entry_text(project: Path, record_id: str) -> str:
    return evidence.evidence_entries(project, record_id=record_id, include_text=True)["entries"][0]["text"]


class TestRecording:
    def test_the_entry_is_labelled_and_has_no_exit_code_line(self, project: Path) -> None:
        record = observe(project, body="rows=42\nnull_share=0.01", source="BigQuery, final_feed.x")
        text = entry_text(project, record)
        assert "- Source: BigQuery, final_feed.x" in text
        assert "- Observation: passed (agent-attested, no process ran)" in text
        assert "rows=42" in text
        # A reader that requires an exit code line never finds one, so an older launcher treats this
        # entry as not selectable and refuses to finish on it: a safe failure, not corruption.
        assert not evidence.EXIT_CODE.findall(text)

    def test_the_result_carries_the_verdict_and_says_it_is_attested(self, project: Path) -> None:
        result = lib.record_observation(  # type: ignore[attr-defined]
            project, "T01", "ok", source="a read", result="FAILED"
        )
        payload = result.as_dict()
        assert payload["kind"] == "observation" and payload["attested"] is True and payload["passed"] is False
        assert payload["owner"] == {"kind": "task", "id": "T01"}
        assert payload["reference"]["anchor"] == f"evidence-{result.record_id[3:]}"
        assert "exit_code" not in payload

    def test_a_closure_step_can_own_an_observation(self, project: Path) -> None:
        result = lib.record_observation(  # type: ignore[attr-defined]
            project, None, "report reads well", step="report", source="reading the report", result="passed"
        )
        assert result.owner_kind == "step" and result.owner_id == "report"

    def test_a_source_containing_the_heading_separator_still_names_the_right_owner(self, project: Path) -> None:
        record = observe(project, source="dashboard — panel 3")
        row = evidence.evidence_entries(project, record_id=record)["entries"][0]
        assert row["owner"] == "T01" and row["selectable"] is True

    def test_a_long_body_is_tailed_like_command_output(self, project: Path) -> None:
        record = observe(project, body="\n".join(f"line {i}" for i in range(50)), tail_lines=5)
        text = entry_text(project, record)
        assert "[45 earlier line(s) elided]" in text and "line 49" in text and "line 0\n" not in text

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"result": "maybe"}, "must be passed or FAILED"),
            ({"source": "   "}, "short one-line label"),
            ({"source": "x" * 201}, "short one-line label"),
            ({"body": "  \n"}, "needs the observed text"),
        ],
    )
    def test_malformed_input_is_refused_and_nothing_is_recorded(
        self, project: Path, kwargs: dict[str, str], message: str
    ) -> None:
        before = (project / "evidence.md").read_text(encoding="utf-8")
        arguments = {"body": "ok", "source": "a read", "result": "passed", **kwargs}
        with pytest.raises(WorkspaceError, match=message):
            lib.record_observation(  # type: ignore[attr-defined]
                project, "T01", arguments["body"], source=arguments["source"], result=arguments["result"]
            )
        assert (project / "evidence.md").read_text(encoding="utf-8") == before

    def test_the_owner_rules_are_the_recorders(self, project: Path) -> None:
        for task, step, message in (
            ("T01", "report", "not both"),
            (None, None, "needs --task"),
            (None, "nonsense", "unknown closure step"),
            ("T99", None, "unknown task"),
        ):
            with pytest.raises(WorkspaceError, match=message):
                lib.record_observation(  # type: ignore[attr-defined]
                    project, task, "ok", step=step, source="a read", result="passed"
                )

    def test_a_source_with_newlines_is_collapsed_to_one_line(self, project: Path) -> None:
        record = observe(project, source="first\nsecond   third")
        assert "- Source: first second third" in entry_text(project, record)


class TestEntryKinds:
    def test_rows_expose_kind_and_a_verdict_without_an_exit_code(self, project: Path) -> None:
        passed, failed, ran = observe(project), observe(project, result="FAILED"), command(project)
        rows = {r["record_id"]: r for r in evidence.evidence_entries(project, task_id="T01")["entries"]}
        assert (rows[passed]["kind"], rows[passed]["passed"], rows[passed]["exit_code"]) == ("observation", True, None)
        verdict = (rows[failed]["kind"], rows[failed]["passed"], rows[failed]["selectable"])
        assert verdict == ("observation", False, True)
        assert (rows[ran]["kind"], rows[ran]["passed"], rows[ran]["exit_code"]) == ("command", True, 0)

    def test_an_entry_claiming_both_an_exit_code_and_an_observation_selects_as_neither(self, project: Path) -> None:
        record = observe(project)
        evidence_file = project / "evidence.md"
        text = evidence_file.read_text(encoding="utf-8")
        marker = "- Observation: passed (agent-attested, no process ran)"
        evidence_file.write_text(text.replace(marker, marker + "\n- Exit code: 0 (passed)"), encoding="utf-8")
        row = evidence.evidence_entries(project, record_id=record)["entries"][0]
        assert row["selectable"] is False and row["passed"] is None
        start(project)
        with pytest.raises(WorkspaceError, match="malformed or ambiguous"):
            operations.task_operation(
                project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[record]
            )

    def test_command_entries_are_unchanged(self, project: Path) -> None:
        row = evidence.evidence_entries(project, record_id=command(project, code=3))["entries"][0]
        assert (row["kind"], row["exit_code"], row["passed"], row["selectable"]) == ("command", 3, False, True)


class TestFinishing:
    def test_an_observation_only_finish_succeeds_and_leaves_an_attested_note(self, project: Path) -> None:
        start(project)
        record = observe(project)
        result = operations.task_operation(
            project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[record]
        )
        assert result["attested"] is True and result["observations"] == [record]
        state = json.loads((project / "project.json").read_text(encoding="utf-8"))
        task = next(t for t in state["tasks"] if t["id"] == "T01")
        assert task["status"] == "DONE" and task["evidence"][-1]["anchor"] == f"evidence-{record[3:]}"
        note = (project / "tasks" / "T01.md").read_text(encoding="utf-8")
        assert "Attested finish" in note and record in note and "no command evidence" in note

    def test_a_mixed_finish_is_not_attested_and_writes_no_attested_note(self, project: Path) -> None:
        start(project)
        ran, saw = command(project), observe(project)
        result = operations.task_operation(
            project, "finish", "T01", expected_revision=revision(project),
            evidence_record_ids=[ran], observation_record_ids=[saw],
        )
        assert result["attested"] is False and result["observations"] == [saw]
        assert not (project / "tasks" / "T01.md").exists()

    def test_evidence_refuses_an_observation_and_observation_refuses_a_command(self, project: Path) -> None:
        start(project)
        saw, ran = observe(project), command(project)
        with pytest.raises(WorkspaceError, match="agent-attested observation, not command evidence"):
            operations.task_operation(
                project, "finish", "T01", expected_revision=revision(project), evidence_record_ids=[saw]
            )
        with pytest.raises(WorkspaceError, match="command result, not an observation"):
            operations.task_operation(
                project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[ran]
            )

    def test_a_refused_finish_changes_nothing(self, project: Path) -> None:
        start(project)
        saw = observe(project)
        before = revision(project)
        with pytest.raises(WorkspaceError):
            operations.task_operation(
                project, "finish", "T01", expected_revision=before, evidence_record_ids=[saw]
            )
        assert revision(project) == before and not (project / "tasks" / "T01.md").exists()

    def test_a_failed_observation_cannot_finish_a_task(self, project: Path) -> None:
        start(project)
        bad = observe(project, result="FAILED")
        with pytest.raises(WorkspaceError, match="did not pass"):
            operations.task_operation(
                project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[bad]
            )

    def test_an_observation_for_another_task_is_refused(self, project: Path) -> None:
        start(project)
        other = observe(project, task="T02")
        with pytest.raises(WorkspaceError, match="belongs to T02"):
            operations.task_operation(
                project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[other]
            )

    def test_finishing_on_nothing_is_still_refused(self, project: Path) -> None:
        start(project)
        with pytest.raises(WorkspaceError, match="at least one explicit evidence record"):
            operations.task_operation(project, "finish", "T01", expected_revision=revision(project))

    def test_a_failed_observation_before_a_pass_triggers_the_lesson_hint(self, project: Path) -> None:
        start(project)
        observe(project, result="FAILED")
        good = observe(project)
        result = operations.task_operation(
            project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[good]
        )
        assert "lesson_hint" in result

    def test_a_backfilled_finish_can_rest_on_an_observation(self, project: Path) -> None:
        saw = observe(project)
        result = operations.task_operation(
            project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[saw],
            backfill=True, note="the read was done while drafting",
        )
        assert result["backfilled"] is True and result["attested"] is True
        note = (project / "tasks" / "T01.md").read_text(encoding="utf-8")
        assert "Backfilled" in note and "Attested finish" in note

    def test_the_same_observation_finishing_again_is_the_same_note(self, project: Path) -> None:
        start(project)
        saw = observe(project)
        operations.task_operation(
            project, "finish", "T01", expected_revision=revision(project), observation_record_ids=[saw]
        )
        assert (project / "tasks" / "T01.md").read_text(encoding="utf-8").count("Attested finish") == 1


class TestGraphCounting:
    ENTRIES = (
        "## T01 — observation: a read\n\n- Record ID: ev-{a}\n- Recorded: 2026-10-02T10:00:00+02:00\n"
        "- Observation: FAILED (agent-attested, no process ran)\n\nobserved (tail):\n\n```\nnothing\n```\n"
        "\n## T01 — observation: another read\n\n- Recorded: 2026-10-02T10:05:00+02:00\n"
        "- Observation: passed (agent-attested, no process ran)\n\nobserved (tail):\n\n```\n"
        "- Exit code: 1 (FAILED)\n```\n"
        "\n## T02 — python -c pass\n\n- Recorded: 2026-10-02T10:10:00+02:00\n- Exit code: 2 (FAILED)\n"
    )

    def spans(self) -> dict[str, Any]:
        return lib.parse_evidence_spans(self.ENTRIES.format(a="0" * 32))

    def test_a_failed_observation_counts_as_a_failure_and_a_passed_one_does_not(self) -> None:
        span = self.spans()["T01"]
        assert (span.entries, span.observations, span.failed) == (2, 2, 1)

    def test_exit_code_text_inside_an_observation_body_is_not_counted(self) -> None:
        # Alone on purpose. Mixed with a FAILED observation the totals can agree by coincidence: a reader
        # that miscounts the quoted line and one that misses the real failure both report one.
        quoted = (
            "## T03 \u2014 observation: a read\n\n- Recorded: 2026-10-02T10:00:00+02:00\n"
            "- Observation: passed (agent-attested, no process ran)\n\nobserved (tail):\n\n```\n"
            "- Exit code: 1 (FAILED)\n```\n"
        )
        span = lib.parse_evidence_spans(quoted)["T03"]
        assert (span.entries, span.observations, span.failed) == (1, 1, 0)

    def test_command_entries_still_count_by_exit_code(self) -> None:
        span = self.spans()["T02"]
        assert (span.entries, span.observations, span.failed) == (1, 0, 1)

    def test_the_rendering_says_checks_only_where_observations_exist(self) -> None:
        state = {
            "project": "2026-01-01-001", "title": "t", "status": "EXECUTING", "revision": 3,
            "tasks": [
                {"id": "T01", "name": "a", "status": "DONE", "depends_on": [], "evidence": [],
                 "outputs": [], "effect": {"kind": "none"}, "authorization": {"status": "not_required"}},
                {"id": "T02", "name": "b", "status": "DONE", "depends_on": [], "evidence": [],
                 "outputs": [], "effect": {"kind": "none"}, "authorization": {"status": "not_required"}},
            ],
        }
        graph = lib.build_task_graph(state, self.ENTRIES.format(a="0" * 32))
        rendered = lib.render_task_graph(graph)
        assert "Non-zero exit codes or failed observations recorded:" in rendered
        assert "T01  1 of 2 recorded checks failed" in rendered
        assert "T02  1 of 1 recorded commands failed" in rendered


class TestCommandLine:
    def test_record_observation_through_the_cli_with_a_body_and_with_stdin(self, project: Path) -> None:
        code, stdout, _ = invoke([
            "record-observation", str(project), "--task", "T01", "--source", "a query",
            "--result", "passed", "--body", "42 rows", "--json",
        ])
        first = json.loads(stdout)
        assert code == 0 and first["kind"] == "observation" and first["passed"] is True
        code, stdout, _ = invoke(
            ["record-observation", str(project), "--task", "T01", "--source", "piped", "--result", "passed",
             "--body-file", "-"],
            stdin="piped text\n",
        )
        assert code == 0 and "agent-attested observation" in stdout
        assert "piped text" in (project / "evidence.md").read_text(encoding="utf-8")

    def test_a_failed_observation_exits_one_and_says_it_is_not_a_pass(self, project: Path) -> None:
        code, _, stderr = invoke([
            "record-observation", str(project), "--task", "T01", "--source", "a query",
            "--result", "FAILED", "--body", "empty result",
        ])
        assert code == 1 and "FAILED observation" in stderr and "not recording it as a pass" in stderr
        code, stdout, _ = invoke([
            "record-observation", str(project), "--task", "T01", "--source", "a query",
            "--result", "FAILED", "--body", "empty", "--json",
        ])
        assert code == 1 and json.loads(stdout)["passed"] is False

    def test_a_refused_observation_exits_one_with_a_message(self, project: Path) -> None:
        code, _, stderr = invoke([
            "record-observation", str(project), "--task", "T99", "--source", "a query",
            "--result", "passed", "--body", "x",
        ])
        assert code == 1 and "unknown task" in stderr

    def test_task_finish_observation_through_the_cli(self, project: Path) -> None:
        start(project)
        record = observe(project)
        code, stdout, _ = invoke([
            "task", str(project), "finish", "T01", "--observation", record,
            "--expected-revision", str(revision(project)),
        ])
        result = json.loads(stdout)
        assert code == 0 and result["attested"] is True and result["observations"] == [record]
