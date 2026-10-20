"""The alignment gate for new projects: what counts as new, and what proves the tool confirmed.

Across 85 projects the only thing a project needed to leave ALIGNING was a legal transition: the
spec, the agreed planning document and the confirmations were warnings at most. The gate enforces them
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
        name = "spec" if kind == "requirements" else "planning"
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
        (project / "planning.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        spec_token = self.confirm(project, "requirements", "R1", "confirm-r1")
        arch_token = self.confirm(project, "planning", "A1", "confirm-a1")
        spec = (project / "spec.md").read_text(encoding="utf-8")
        found = lib.recorded_confirmations(spec)
        assert found == {"requirements": [("R1", spec_token)], "planning": [("A1", arch_token)]}
        assert lib.confirmation_marker("planning", "A1", arch_token) in (
            (project / "planning.md").read_text(encoding="utf-8")
        ), "the planning confirmation block carries the same marker"

    def test_a_hand_typed_decision_is_not_a_recorded_confirmation(self, project: Path) -> None:
        from workspace_documents import append_record

        append_record(
            project, "decision",
            "kind: planning\nreview_id: A1\nproposal_sha256: " + "0" * 64
            + "\nresponse: yes\nsource: me\nscope: all",
            entry_id="looks-like-one",
        )
        assert lib.recorded_confirmations((project / "spec.md").read_text(encoding="utf-8")) == {
            "requirements": [], "planning": [],
        }

    def test_the_finder_ignores_a_marker_with_a_malformed_sha_or_kind(self) -> None:
        text = (
            "<!-- research-confirmation: kind=architecture review_id=A1 proposal_sha256=short -->\n"
            "<!-- research-confirmation: kind=design review_id=A1 proposal_sha256=" + "a" * 64 + " -->\n"
            "<!-- research-confirmation: kind=requirements review_id=R2 proposal_sha256=" + "b" * 64 + " -->\n"
        )
        assert lib.recorded_confirmations(text) == {"requirements": [("R2", "b" * 64)], "planning": []}

    def test_confirming_closes_the_lifecycle_as_before(self, project: Path) -> None:
        # The marker must not disturb what confirm already did: agreed status and the spec link.
        fill_spec(project)
        (project / "planning.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        self.confirm(project, "planning", "A1", "confirm-a1")
        assert lib.planning_status((project / "planning.md").read_text(encoding="utf-8")) == "agreed"
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert "Agreed planning: [revision A1](planning.md)." in spec


PLANNING = "# A1\n\nStatus: draft\n\n## Modules\n\nOne parser.\n"


def plan_patch(status: str = "PLANNING") -> dict[str, Any]:
    from tests.plugins.research.project.test_workflow_automation import task

    return {"status": status, "tasks": [task("T01")]}


def align(project: Path, *, spec: bool = True, planning: bool = True, requirements: bool = True,
          agreed: bool = True) -> None:
    """Bring a project to the fully aligned state, or leave out one fact."""
    if spec:
        fill_spec(project)
    if planning:
        (project / "planning.md").write_text(PLANNING, encoding="utf-8")
    confirmer = TestConfirmationMarker()
    if requirements:
        confirmer.confirm(project, "requirements", "R1", "confirm-r1")
    if planning and agreed:
        confirmer.confirm(project, "planning", "A1", "confirm-a1")


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
            ({"planning": False}, "planning.md missing"),
            ({"agreed": False}, "planning.md is a draft"),
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
        (new_project / "planning.md").write_text(PLANNING.replace("draft", "agreed"), encoding="utf-8")
        with pytest.raises(lib.WorkspaceError, match="no requirements confirmation"):
            self.leave(new_project)

    def test_a_planning_marker_must_name_a_review_present_in_the_document(self, new_project: Path) -> None:
        align(new_project)
        path = new_project / "planning.md"
        path.write_text(path.read_text(encoding="utf-8").replace("A1", "A2"), encoding="utf-8")
        with pytest.raises(lib.WorkspaceError, match=r"review identifier present in planning\.md"):
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
        assert report.valid and any("planning.md is missing" in w for w in report.warnings)

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
        path = new_project / "planning.md"
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


class TestAgreementIsBoundToContent:
    """Confirming A1 and then rewriting it left the review id and status in place, and PLANNING still succeeded.

    The gate now compares the digest recorded when the user agreed with the digest of what the
    documents hold, ignoring only what the tool itself writes: decision history, the planning document
    link, the status word, the confirmation block, blank lines.
    """

    @pytest.fixture
    def aligned(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        align(project)
        return project

    def leave(self, project: Path) -> dict[str, Any]:
        from workspace_session import update_project_data

        return update_project_data(project, plan_patch(), expected_revision=state_of(project)["revision"])

    def edit_planning(self, project: Path, transform: Any) -> dict[str, Any]:
        from workspace_documents import document_snapshot, edit_document

        text = (project / "planning.md").read_text(encoding="utf-8")
        token = document_snapshot(project, "planning")["document_sha256"]
        return edit_document(project, "planning", expected_sha256=token, body=transform(text))

    def edit_spec(self, project: Path, heading: str, body: str) -> dict[str, Any]:
        from workspace_documents import document_snapshot, edit_document

        token = document_snapshot(project, "spec")["document_sha256"]
        return edit_document(project, "spec", expected_sha256=token, sections={heading: body})

    def test_a_planning_document_rewritten_after_agreement_cannot_leave_aligning(self, aligned: Path) -> None:
        self.edit_planning(aligned, lambda text: text.replace("One parser.", "A different design entirely."))
        text = (aligned / "planning.md").read_text(encoding="utf-8")
        assert "A1" in text and lib.planning_status(text) == "agreed", "status and review id still look agreed"
        before = state_of(aligned)["revision"]
        with pytest.raises(lib.WorkspaceError, match=r"planning\.md changed after its planning confirmation"):
            self.leave(aligned)
        assert state_of(aligned)["revision"] == before

    def test_a_specification_rewritten_after_agreement_cannot_leave_aligning(self, aligned: Path) -> None:
        self.edit_spec(aligned, "In scope", "Something the user never saw.")
        with pytest.raises(lib.WorkspaceError, match="the specification changed after its requirements confirmation"):
            self.leave(aligned)

    def test_confirming_the_current_text_again_repairs_it(self, aligned: Path) -> None:
        self.edit_planning(aligned, lambda text: text.replace("One parser.", "A different design entirely."))
        with pytest.raises(lib.WorkspaceError, match="changed after"):
            self.leave(aligned)
        confirmer = TestConfirmationMarker()
        confirmer.confirm(aligned, "planning", "A1", "confirm-a1-again")
        assert self.leave(aligned)["status"] == "PLANNING"

    def test_the_tools_own_writes_do_not_unconfirm_anything(self, aligned: Path) -> None:
        from workspace_documents import append_record

        append_record(aligned, "decision", "A later decision.", entry_id="later-decision")
        spec = (aligned / "spec.md").read_text(encoding="utf-8")
        assert lib.proposal_digest("requirements", spec) == lib.recorded_proposals(spec)["requirements"][-1][1]
        self.edit_planning(aligned, lambda text: text.replace("\n\n", "\n\n\n").replace("Modules", "Modules  "))
        assert self.leave(aligned)["status"] == "PLANNING"

    def test_a_confirmation_that_predates_content_binding_is_refused_with_a_repair(self, aligned: Path) -> None:
        spec_path = aligned / "spec.md"
        lines = spec_path.read_text(encoding="utf-8").splitlines(keepends=True)
        spec_path.write_text("".join(line for line in lines if "research-proposal" not in line), encoding="utf-8")
        with pytest.raises(lib.WorkspaceError) as excinfo:
            self.leave(aligned)
        assert str(excinfo.value).count("predates content binding") == 2
        assert "workflow confirm" in str(excinfo.value)

    def test_edits_report_which_confirmations_they_made_stale(self, aligned: Path) -> None:
        result = self.edit_planning(aligned, lambda text: text.replace("One parser.", "Two parsers."))
        assert [item.split(" confirmation")[0] for item in result["stale_confirmations"]] == ["the planning"]
        assert "A1" in result["stale_confirmations"][0]
        spec_result = self.edit_spec(aligned, "In scope", "Changed scope.")
        assert len(spec_result["stale_confirmations"]) == 2
        TestConfirmationMarker().confirm(aligned, "requirements", "R1", "confirm-r1-again")
        TestConfirmationMarker().confirm(aligned, "planning", "A1", "confirm-a1-again")
        quiet = self.edit_spec(aligned, "In scope", "Changed scope.")
        assert "stale_confirmations" not in quiet

    def test_a_round_reports_a_confirmation_it_made_stale_until_it_is_repaired(self, aligned: Path) -> None:
        payload = request(aligned, "round-stale", sections={"Out of scope": "Newly excluded work."})
        [stale] = workflow(aligned, "round", payload)["stale_confirmations"]
        assert stale.startswith("the requirements confirmation R1")
        decisions = request(aligned, "round-decision", decisions=[{"id": "just-a-note", "body": "A note."}])
        assert "stale_confirmations" in workflow(aligned, "round", decisions), "it persists until repaired"

    def test_an_unconfirmed_draft_has_nothing_to_go_stale(self, project: Path) -> None:
        fill_spec(project)
        (project / "planning.md").write_text(PLANNING, encoding="utf-8")
        payload = request(project, "round-draft", sections={"Out of scope": "Anything."})
        assert "stale_confirmations" not in workflow(project, "round", payload)

    def test_editing_another_document_reports_nothing(self, aligned: Path) -> None:
        from workspace_documents import edit_document

        result = edit_document(aligned, "reflection", expected_sha256="missing", body="# Reflection\n\nNothing yet.")
        assert "stale_confirmations" not in result


class TestProposalDigest:
    def test_the_planning_status_word_and_confirmation_block_are_ignored(self) -> None:
        draft = "# A1\n\nStatus: draft\n\nOne parser.\n"
        agreed = draft.replace("draft", "agreed") + "\nConfirmation (2026-10-02T10:00:00+02:00):\n\nresponse: yes\n"
        assert lib.proposal_digest("planning", draft) == lib.proposal_digest("planning", agreed)
        assert lib.proposal_digest("planning", draft) != lib.proposal_digest("planning", draft + "More.\n")

    def test_a_planning_document_without_a_status_line_is_digested_as_written(self) -> None:
        assert lib.proposal_digest("planning", "# A1\n\nBody.\n") != lib.proposal_digest(
            "planning", "# A1\n\nOther body.\n"
        )

    def test_requirements_are_the_current_specification_alone(self) -> None:
        base = "# T\n\n## Current specification\n\n### In scope\n\nWork.\n\n## Decision history\n\n- one\n"
        grown = base + "- two\n"
        linked = base.replace("Work.\n", "Work.\n\nAgreed planning: [revision A1](planning.md).\n")
        assert lib.proposal_digest("requirements", base) == lib.proposal_digest("requirements", grown)
        assert lib.proposal_digest("requirements", base) == lib.proposal_digest("requirements", linked)
        assert lib.proposal_digest("requirements", base) != lib.proposal_digest(
            "requirements", base.replace("Work.", "Other work.")
        )

    def test_a_document_without_a_specification_digests_as_empty(self) -> None:
        assert lib.proposal_digest("requirements", "# T\n") == lib.proposal_digest("requirements", "# U\n\nx\n")


class TestAlignmentConfirmation:
    """One reply that confirms requirements and design together, for a proposal small enough to read at once."""

    @pytest.fixture
    def drafted(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        fill_spec(project)
        (project / "planning.md").write_text(PLANNING, encoding="utf-8")
        return project

    def tokens(self, project: Path) -> dict[str, str]:
        return {
            "requirements_sha256": lib.document_sha256((project / "spec.md").read_text(encoding="utf-8")),
            "planning_sha256": lib.document_sha256((project / "planning.md").read_text(encoding="utf-8")),
        }

    def confirm(self, project: Path, identity: str = "align-1", **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "kind": "alignment", **self.tokens(project), "review_id": "A1", "response": "Yes, go ahead",
            "source": "user reply", "scope": "requirements and design together",
            "continuation": {"next": "Plan", "questions": ""},
        }
        fields.update(overrides)
        fields = {key: value for key, value in fields.items() if value is not None}
        return workflow(project, "confirm", request(project, identity, **fields))

    def files(self, project: Path) -> dict[str, str]:
        return {
            name: (project / name).read_text(encoding="utf-8")
            for name in ("spec.md", "planning.md", "project.json")
        }

    def test_one_reply_satisfies_the_gate_for_both_documents(self, drafted: Path) -> None:
        from workspace_session import update_project_data

        tokens = self.tokens(drafted)
        self.confirm(drafted)
        spec = (drafted / "spec.md").read_text(encoding="utf-8")
        planning = (drafted / "planning.md").read_text(encoding="utf-8")
        assert lib.recorded_confirmations(spec) == {
            "requirements": [("A1", tokens["requirements_sha256"])],
            "planning": [("A1", tokens["planning_sha256"])],
        }
        proposals = lib.recorded_proposals(spec)
        assert proposals["requirements"][0][1] == lib.proposal_digest("requirements", spec)
        assert proposals["planning"][0][1] == lib.proposal_digest("planning", planning)
        assert lib.planning_status(planning) == "agreed"
        assert "Agreed planning: [revision A1](planning.md)." in spec
        assert "align-1-requirements" in spec and "align-1-planning" in spec
        patched = update_project_data(drafted, plan_patch(), expected_revision=state_of(drafted)["revision"])
        assert patched["status"] == "PLANNING"

    def test_editing_after_an_alignment_confirmation_is_still_caught(self, drafted: Path) -> None:
        from workspace_documents import document_snapshot, edit_document
        from workspace_session import update_project_data

        self.confirm(drafted)
        token = document_snapshot(drafted, "planning")["document_sha256"]
        text = (drafted / "planning.md").read_text(encoding="utf-8")
        edit_document(drafted, "planning", expected_sha256=token, body=text.replace("One parser.", "Two."))
        with pytest.raises(lib.WorkspaceError, match="changed after its planning confirmation"):
            update_project_data(drafted, plan_patch(), expected_revision=state_of(drafted)["revision"])

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"requirements_sha256": "0" * 64}, "proposal token changed"),
            ({"planning_sha256": "0" * 64}, "proposal token changed"),
            ({"proposal_sha256": "0" * 64}, "not proposal_sha256"),
            ({"planning_sha256": None}, "planning_sha256"),
            ({"requirements_sha256": None}, "requirements_sha256"),
            ({"review_id": "Z9"}, "declare its status and reviewed identifier"),
            ({"response": None}, "response"),
        ],
    )
    def test_each_refusal_leaves_every_file_unchanged(
        self, drafted: Path, overrides: dict[str, Any], fragment: str
    ) -> None:
        before = self.files(drafted)
        with pytest.raises(lib.WorkspaceError, match=fragment):
            self.confirm(drafted, **overrides)
        assert self.files(drafted) == before

    def test_the_single_document_kinds_refuse_the_alignment_tokens(self, drafted: Path) -> None:
        before = self.files(drafted)
        with pytest.raises(lib.WorkspaceError, match="only kind alignment"):
            self.confirm(drafted, kind="requirements", proposal_sha256=self.tokens(drafted)["requirements_sha256"])
        with pytest.raises(lib.WorkspaceError, match="confirmation kind must be requirements, planning or"):
            self.confirm(drafted, kind="everything")
        assert self.files(drafted) == before

    def test_a_retry_with_the_same_input_adds_nothing(self, drafted: Path) -> None:
        payload = request(
            drafted, "align-retry", kind="alignment", **self.tokens(drafted), review_id="A1", response="Yes",
            source="user reply", scope="both", continuation={"next": "Plan"},
        )
        first = workflow(drafted, "confirm", payload)
        assert workflow(drafted, "confirm", payload) == first
        spec = (drafted / "spec.md").read_text(encoding="utf-8")
        assert spec.count("<!-- research-confirmation:") == 2 and spec.count("<!-- research-proposal:") == 2


class TestTheNamesBeforeThePlanningSplit:
    """What older records, habits and callers say still works, and means the planning document."""

    def test_markers_written_as_architecture_count_as_planning(self) -> None:
        text = (
            "<!-- research-confirmation: kind=architecture review_id=A1 proposal_sha256=" + "a" * 64 + " -->\n"
            "<!-- research-proposal: kind=architecture review_id=A1 content_sha256=" + "b" * 64 + " -->\n"
            "<!-- research-confirmation: kind=planning review_id=A2 proposal_sha256=" + "c" * 64 + " -->\n"
            "<!-- research-proposal: kind=planning review_id=A2 content_sha256=" + "d" * 64 + " -->\n"
        )
        assert lib.recorded_confirmations(text) == {
            "requirements": [], "planning": [("A1", "a" * 64), ("A2", "c" * 64)],
        }
        assert lib.recorded_proposals(text) == {
            "requirements": [], "planning": [("A1", "b" * 64), ("A2", "d" * 64)],
        }

    def test_the_old_agreed_line_does_not_change_the_requirements_digest(self) -> None:
        base = "# T\n\n## Current specification\n\n### In scope\n\nWork.\n"
        old = base.replace("Work.\n", "Work.\n\nAgreed architecture: [revision A1](architecture.md).\n")
        assert lib.proposal_digest("requirements", base) == lib.proposal_digest("requirements", old)

    def test_confirm_kind_architecture_is_a_planning_confirmation(self, project: Path) -> None:
        fill_spec(project)
        (project / "planning.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        TestConfirmationMarker().confirm(project, "architecture", "A1", "confirm-a1")
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert "kind=planning review_id=A1" in spec
        assert "kind=architecture" not in spec
        assert lib.planning_status((project / "planning.md").read_text(encoding="utf-8")) == "agreed"

    def test_a_legacy_layout_is_agreed_in_place_and_linked_by_its_own_name(self, project: Path) -> None:
        fill_spec(project)
        (project / "architecture.md").write_text("# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n", encoding="utf-8")
        token = lib.document_sha256((project / "architecture.md").read_text(encoding="utf-8"))
        workflow(
            project,
            "confirm",
            request(
                project, "confirm-legacy", kind="planning", proposal_sha256=token, review_id="A1",
                response="I confirm", source="user reply", scope="planning",
                continuation={"next": "Plan", "questions": ""},
                tokens={
                    "spec": lib.document_sha256((project / "spec.md").read_text(encoding="utf-8")),
                    "planning": token,
                },
            ),
        )
        assert not (project / "planning.md").exists()
        assert lib.planning_status((project / "architecture.md").read_text(encoding="utf-8")) == "agreed"
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert "Agreed planning: [revision A1](architecture.md)." in spec

    def test_alignment_takes_the_old_token_name_but_not_both(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        fill_spec(project)
        (project / "planning.md").write_text(PLANNING, encoding="utf-8")
        tokens = TestAlignmentConfirmation().tokens(project)
        common = {
            "kind": "alignment", "review_id": "A1", "response": "Yes", "source": "user reply", "scope": "both",
            "continuation": {"next": "Plan", "questions": ""}, "requirements_sha256": tokens["requirements_sha256"],
        }
        with pytest.raises(lib.WorkspaceError, match="not both"):
            workflow(
                project, "confirm",
                request(project, "both-names", **common, planning_sha256=tokens["planning_sha256"],
                        architecture_sha256=tokens["planning_sha256"]),
            )
        old_name = request(project, "old-name", **common, architecture_sha256=tokens["planning_sha256"])
        workflow(project, "confirm", old_name)
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert [review for review, _ in lib.recorded_confirmations(spec)["planning"]] == ["A1"]

    def test_round_refuses_the_design_key_in_a_legacy_layout_and_takes_planning(self, project: Path) -> None:
        legacy = "# A1\n\nStatus: draft\n\n## Modules\n\nOne.\n"
        (project / "architecture.md").write_text(legacy, encoding="utf-8")
        continuation = {"next": "Review", "questions": ""}
        with pytest.raises(lib.WorkspaceError, match="use the document id planning"):
            workflow(
                project, "round", request(project, "design-key", architecture="# Design\n", continuation=continuation)
            )
        assert (project / "architecture.md").read_text(encoding="utf-8") == legacy
        assert not (project / "artifacts").exists()
        tokens = {"planning": lib.document_sha256(legacy)}
        revised = "# A2\n\nStatus: draft\n\n## Modules\n\nTwo.\n"
        planning_round = request(project, "planning-key", planning=revised, tokens=tokens, continuation=continuation)
        workflow(project, "round", planning_round)
        assert (project / "architecture.md").read_text(encoding="utf-8") == revised
        assert not (project / "planning.md").exists()

    def test_round_writes_the_design_key_under_artifacts_when_there_is_no_legacy_file(self, project: Path) -> None:
        continuation = {"next": "Review", "questions": ""}
        workflow(project, "round", request(project, "design", architecture="# Design\n", continuation=continuation))
        assert (project / "artifacts" / "architecture.md").read_text(encoding="utf-8") == "# Design\n"
        assert not (project / "planning.md").exists()
        assert not (project / "architecture.md").exists()
