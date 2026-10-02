"""The alignment gate for new projects: what counts as new, and what proves the tool confirmed.

Across 85 projects the only thing a project needed to leave ALIGNING was a legal transition: the
spec, the agreed architecture and the confirmations were warnings at most. The gate enforces them
for projects created on or after a cutoff date, so no existing project changes shape and no older
launcher is locked out by the gate itself. These tests cover the foundations (the cutoff, the
confirmation marker, the test fixtures); the gate's own transitions are tested further down once it
exists.

New names are looked up when a test runs, so a build without the feature fails each test on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_workflows import workflow

from tests.conftest import backdate_project
from tests.plugins.research.project.test_lifecycle_commands import REFLECTION, request
from tests.plugins.research.project.test_workflow_automation import fill_spec

CUTOFF = "2026-10-15T00:00:00+00:00"


def state_with(created: object) -> dict[str, Any]:
    return {"created": created}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Gate", working_directory=target, create_root=True)


class TestIsNewProject:
    @pytest.fixture(autouse=True)
    def cutoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", CUTOFF)

    @pytest.mark.parametrize(
        ("created", "new"),
        [
            ("2026-10-14T23:59:59+00:00", False),
            ("2026-10-15T00:00:00+00:00", True),  # on the instant counts
            ("2026-10-15T01:59:59+02:00", False),  # the same instant minus a second, in another zone
            ("2026-10-15T02:00:00+02:00", True),
            ("2026-10-16T00:00:00Z", True),  # Z suffix
            ("2020-01-01T00:00:00+00:00", False),
        ],
    )
    def test_the_comparison_is_by_instant_not_by_text(self, created: str, new: bool) -> None:
        assert lib.is_new_project(state_with(created)) is new

    @pytest.mark.parametrize("created", ["2027-01-01T00:00:00", "not a date", "", None, 7, "2027-01-01"])
    def test_anything_that_cannot_be_judged_is_legacy(self, created: object) -> None:
        assert lib.is_new_project(state_with(created)) is False

    def test_a_malformed_cutoff_never_turns_enforcement_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "someday")
        assert lib.is_new_project(state_with("2030-01-01T00:00:00+00:00")) is False
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2026-10-15T00:00:00")  # naive cutoff
        assert lib.is_new_project(state_with("2030-01-01T00:00:00+00:00")) is False


class TestFixtures:
    def test_every_test_project_is_legacy_by_default(self, project: Path) -> None:
        # The autouse fixture holds the cutoff in the far future; a project created now is not new.
        assert lib.GATES_ENFORCED_FROM == "2099-01-01T00:00:00+00:00"
        state = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert lib.is_new_project(state) is False

    def test_a_project_can_be_made_new_or_legacy_on_disk(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", CUTOFF)
        backdate_project(project, "2030-01-01T00:00:00+00:00")
        assert lib.is_new_project(json.loads((project / "project.json").read_text(encoding="utf-8"))) is True
        backdate_project(project)
        assert lib.is_new_project(json.loads((project / "project.json").read_text(encoding="utf-8"))) is False
        assert lib.validate_project(project).valid, "a backdated project is still a valid project"


class TestConfirmationMarker:
    def confirm(self, project: Path, kind: str, review_id: str, identity: str) -> str:
        name = "spec" if kind == "requirements" else "architecture"
        token = lib.document_sha256((project / f"{name}.md").read_text(encoding="utf-8"))
        workflow(
            project,
            "confirm",
            request(
                project, identity, kind=kind, proposal_sha256=token, review_id=review_id,
                response="I confirm", source="user reply", scope=kind, continuation={"next": "Plan", "questions": ""},
            ),
        )
        return token

    def test_confirm_writes_one_marker_per_confirmation_into_the_spec(self, project: Path) -> None:
        fill_spec(project)
        (project / "architecture.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        spec_token = self.confirm(project, "requirements", "R1", "confirm-r1")
        arch_token = self.confirm(project, "architecture", "A1", "confirm-a1")
        spec = (project / "spec.md").read_text(encoding="utf-8")
        found = lib.recorded_confirmations(spec)
        assert found == {"requirements": [("R1", spec_token)], "architecture": [("A1", arch_token)]}
        assert lib.confirmation_marker("architecture", "A1", arch_token) in (
            (project / "architecture.md").read_text(encoding="utf-8")
        ), "the architecture confirmation block carries the same marker"

    def test_a_hand_typed_decision_is_not_a_recorded_confirmation(self, project: Path) -> None:
        from workspace_documents import append_record

        append_record(
            project, "decision",
            "kind: architecture\nreview_id: A1\nproposal_sha256: " + "0" * 64
            + "\nresponse: yes\nsource: me\nscope: all",
            entry_id="looks-like-one",
        )
        assert lib.recorded_confirmations((project / "spec.md").read_text(encoding="utf-8")) == {
            "requirements": [], "architecture": [],
        }

    def test_the_finder_ignores_a_marker_with_a_malformed_sha_or_kind(self) -> None:
        text = (
            "<!-- research-confirmation: kind=architecture review_id=A1 proposal_sha256=short -->\n"
            "<!-- research-confirmation: kind=design review_id=A1 proposal_sha256=" + "a" * 64 + " -->\n"
            "<!-- research-confirmation: kind=requirements review_id=R2 proposal_sha256=" + "b" * 64 + " -->\n"
        )
        assert lib.recorded_confirmations(text) == {"requirements": [("R2", "b" * 64)], "architecture": []}

    def test_confirming_closes_the_lifecycle_as_before(self, project: Path) -> None:
        # The marker must not disturb what confirm already did: agreed status and the spec link.
        fill_spec(project)
        (project / "architecture.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        self.confirm(project, "architecture", "A1", "confirm-a1")
        assert lib.architecture_status((project / "architecture.md").read_text(encoding="utf-8")) == "agreed"
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert "Agreed architecture: [revision A1](architecture.md)." in spec


ARCHITECTURE = "# A1\n\nStatus: draft\n\n## Modules\n\nOne parser.\n"


def plan_patch(status: str = "PLANNING") -> dict[str, Any]:
    from tests.plugins.research.project.test_workflow_automation import task

    return {"status": status, "tasks": [task("T01")]}


def align(project: Path, *, spec: bool = True, architecture: bool = True, requirements: bool = True,
          agreed: bool = True) -> None:
    """Bring a project to the fully aligned state, or leave out one fact."""
    if spec:
        fill_spec(project)
    if architecture:
        (project / "architecture.md").write_text(ARCHITECTURE, encoding="utf-8")
    confirmer = TestConfirmationMarker()
    if requirements:
        confirmer.confirm(project, "requirements", "R1", "confirm-r1")
    if architecture and agreed:
        confirmer.confirm(project, "architecture", "A1", "confirm-a1")


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))


class TestGate:
    """A new project may leave ALIGNING only when its alignment is on record; nothing else is gated."""

    @pytest.fixture
    def new_project(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        assert lib.is_new_project(state_of(project))
        return project

    def leave(self, project: Path, status: str = "PLANNING") -> dict[str, Any]:
        from workspace_session import update_project_data

        return update_project_data(project, plan_patch(status), expected_revision=state_of(project)["revision"])

    def test_a_fully_aligned_new_project_may_plan(self, new_project: Path) -> None:
        align(new_project)
        assert self.leave(new_project)["status"] == "PLANNING"

    def test_an_empty_specification_section_refuses_the_transition(self, new_project: Path) -> None:
        align(new_project)
        spec = new_project / "spec.md"
        text = spec.read_text(encoding="utf-8")
        heading = "### Out of scope\n"
        start = text.index(heading) + len(heading)
        end = text.index("### ", start)
        spec.write_text(text[:start] + "\n" + text[end:], encoding="utf-8")
        with pytest.raises(lib.WorkspaceError, match="specification is incomplete"):
            self.leave(new_project)
        assert state_of(new_project)["status"] == "ALIGNING"

    @pytest.mark.parametrize(
        ("left_out", "expected"),
        [
            ({"architecture": False}, "architecture.md missing"),
            ({"agreed": False}, "architecture.md is a draft"),
            ({"requirements": False}, "no requirements confirmation was recorded"),
        ],
    )
    def test_each_missing_fact_refuses_the_transition_and_names_its_command(
        self, new_project: Path, left_out: dict[str, bool], expected: str
    ) -> None:
        align(new_project, **left_out)
        before = state_of(new_project)["revision"]
        with pytest.raises(lib.WorkspaceError) as caught:
            self.leave(new_project)
        message = str(caught.value)
        assert expected in message and "workflow confirm" in message
        assert "may not leave ALIGNING for PLANNING" in message
        assert state_of(new_project)["revision"] == before and state_of(new_project)["status"] == "ALIGNING"

    def test_a_hand_written_agreed_status_without_the_markers_is_refused(self, new_project: Path) -> None:
        fill_spec(new_project)
        (new_project / "architecture.md").write_text(ARCHITECTURE.replace("draft", "agreed"), encoding="utf-8")
        with pytest.raises(lib.WorkspaceError, match="no requirements confirmation"):
            self.leave(new_project)

    def test_an_architecture_marker_must_name_a_review_present_in_the_document(self, new_project: Path) -> None:
        align(new_project)
        path = new_project / "architecture.md"
        path.write_text(path.read_text(encoding="utf-8").replace("A1", "A2"), encoding="utf-8")
        with pytest.raises(lib.WorkspaceError, match=r"review identifier present in architecture\.md"):
            self.leave(new_project)

    def test_the_dry_run_reports_the_same_refusal_without_writing(self, new_project: Path) -> None:
        from workspace_workflows import preview

        report = preview(new_project, plan_patch(), state_of(new_project)["revision"])
        assert report["valid"] is False
        assert any("may not leave ALIGNING" in error for error in report["errors"])
        assert state_of(new_project)["status"] == "ALIGNING"

    def test_a_legacy_project_is_only_warned_as_before(self, project: Path) -> None:
        # The autouse fixture keeps the cutoff in the future, so this project is legacy.
        from workspace_session import update_project_data

        state = update_project_data(project, plan_patch(), expected_revision=0)
        assert state["status"] == "PLANNING"
        report = lib.validate_project(project)
        assert report.valid and any("architecture.md is missing" in w for w in report.warnings)

    def test_blocked_is_reachable_but_leaving_it_for_planning_is_gated(self, new_project: Path) -> None:
        from workspace_session import update_project_data

        blocked = {"status": "BLOCKED", "tasks": [{**plan_patch()["tasks"][0], "status": "BLOCKED",
                                                    "block_reason": "waiting for the user"}]}
        state = update_project_data(new_project, blocked, expected_revision=0)
        assert state["status"] == "BLOCKED"
        with pytest.raises(lib.WorkspaceError, match="may not leave BLOCKED for PLANNING"):
            update_project_data(
                new_project,
                {"status": "PLANNING", "tasks": [{"id": "T01", "status": "TODO", "block_reason": None}]},
                expected_revision=state["revision"],
            )
        align(new_project)
        state = update_project_data(
            new_project,
            {"status": "PLANNING", "tasks": [{"id": "T01", "status": "TODO", "block_reason": None}]},
            expected_revision=state_of(new_project)["revision"],
        )
        assert state["status"] == "PLANNING"

    def test_a_reopened_project_and_a_mid_execution_draft_are_not_gated(self, new_project: Path) -> None:
        from workspace_operations import task_operation
        from workspace_workflows import workflow

        align(new_project)
        self.leave(new_project)
        task_operation(new_project, "start", "T01", expected_revision=state_of(new_project)["revision"])
        record = lib.record_evidence_result(new_project, "T01", ["python3", "-c", "pass"]).record_id
        # A design revision reopened as a draft while executing: commits must still go through.
        path = new_project / "architecture.md"
        path.write_text(path.read_text(encoding="utf-8").replace("Status: agreed", "Status: draft"), encoding="utf-8")
        task_operation(
            new_project, "finish", "T01", expected_revision=state_of(new_project)["revision"],
            evidence_record_ids=[record],
        )
        assert state_of(new_project)["status"] == "EXECUTING"
        # Reopening a finished project is maintenance, not alignment.
        path.write_text(path.read_text(encoding="utf-8").replace("Status: draft", "Status: agreed"), encoding="utf-8")
        payload = request(new_project, "final", reflection=REFLECTION, continuation={"next": "none"})
        workflow(new_project, "finalize", payload)
        assert state_of(new_project)["status"] == "DONE"
        path.write_text(path.read_text(encoding="utf-8").replace("Status: agreed", "Status: draft"), encoding="utf-8")
        reopened = workflow(
            new_project, "maintenance",
            request(new_project, "reopen", reason="follow-up", continuation={"next": "plan more"}),
        )
        assert reopened["complete"] and state_of(new_project)["status"] == "PLANNING"


class TestGateThroughTheLaunchers:
    """The production cutoff is read by the launcher process; `created` on disk decides what is new."""

    @pytest.mark.parametrize("surface", ["bin", "skills/project/scripts"])
    def test_a_project_dated_after_the_cutoff_is_refused_and_a_backdated_one_is_not(
        self, tmp_path: Path, surface: str
    ) -> None:
        import subprocess

        from tests.conftest import REPO_ROOT

        launcher = REPO_ROOT / "plugins/research" / surface / "research-project"
        target = tmp_path / "target"
        target.mkdir()
        patch = json.dumps(plan_patch())
        outcomes = {}
        for label, created in (("new", "2100-01-01T00:00:00+00:00"), ("legacy", "2020-01-01T00:00:00+00:00")):
            root = tmp_path / f"ws-{label}"
            created_json = subprocess.run(
                [str(launcher), "init", str(root), "--title", label, "--working-directory", str(target),
                 "--create-root", "--json"],
                capture_output=True, text=True, check=True,
            ).stdout
            project = Path(json.loads(created_json)["project_directory"])
            backdate_project(project, created)
            result = subprocess.run(
                [str(launcher), "update", str(project), "-", "--expected-revision", "0"],
                input=patch, capture_output=True, text=True,
            )
            outcomes[label] = (result.returncode, result.stderr)
        assert outcomes["new"][0] == 1 and "may not leave ALIGNING for PLANNING" in outcomes["new"][1]
        assert outcomes["legacy"][0] == 0, outcomes["legacy"][1]


class TestCutoffConstant:
    """The cutoff is set once, at the release that shipped the gate, and is a real past instant."""

    def test_the_cutoff_is_a_timezone_aware_instant_in_the_past(self) -> None:
        from datetime import datetime, timezone

        # The autouse fixture replaced the module attribute; read the shipped value from the source.
        source = (Path(lib.__file__)).read_text(encoding="utf-8")
        shipped = next(line for line in source.splitlines() if line.startswith("GATES_ENFORCED_FROM = "))
        value = shipped.split("=", 1)[1].strip().strip('"')
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert moment.tzinfo is not None
        assert datetime(2026, 10, 1, tzinfo=timezone.utc) <= moment <= datetime.now(timezone.utc), value

    def test_the_release_note_names_the_constant_and_the_release(self) -> None:
        from tests.conftest import REPO_ROOT

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "### 0.21.0" in readme and "GATES_ENFORCED_FROM" in readme
