"""The design document `artifacts/architecture.md`, and the documents that name it.

A project that writes code starts with a design task whose output is `artifacts/architecture.md`;
every later code task keeps it current. The document is never gated, so the checks here are
warnings, for a project created after the split and nothing else: a docs-only plan, a closed
project, an older project and a project still aligning stay quiet.

Two kinds of test live here. The first group calls the validator the way a user meets it. The second
group is the agreement between the shipped documents and the code: the ids a document tells the
agent to read or edit must be ids the tool accepts, and no document may call the agreement document
`architecture.md` outside the one legacy note, because a reader who believes it writes the agreement
into the design file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import manage_workspace
import pytest
import workspace_lib as lib
from workspace_session import update_project_data

from tests.conftest import REPO_ROOT

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
SKILL_DIR = REPO_ROOT / "plugins/research/skills/project"

FULL_DESIGN = (
    "# Design\n\nTemplate: v1\nProfiles: core\n\n"
    + "".join(f"## {number}. {title}\n\nNot applicable: test\n\n" for number, title in (
        (1, "Purpose and quality attributes"), (2, "Context and containers"), (3, "Modules and code layout"),
        (5, "Key flows"), (8, "Failure modes and recovery"), (10, "Evaluation plan"), (12, "Decisions")))
    + "## 13. Implementation status\n\n| Task | Module | Status | Deviation |\n| --- | --- | --- | --- |\n"
    + "| T02 | parser | done | none |\n"
)


def code_task(task_id: str, status: str = "TODO", path: str = "src/app.py") -> dict[str, Any]:
    return {
        "id": task_id, "status": status, "effect": {"kind": "local_write", "description": "edit"},
        "outputs": [{"root": "target", "path": path, "required": True}],
    }


def docs_task(task_id: str) -> dict[str, Any]:
    return code_task(task_id, path="notes/summary.md")


@pytest.fixture(autouse=True)
def split_in_the_past(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "PLANNING_SPLIT_FROM", PAST)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Design", working_directory=target, create_root=True)


def new_state(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"created": "2026-10-20T10:00:00+00:00", "tasks": list(tasks)}


def write_design(project: Path, text: str) -> None:
    path = project / lib.DESIGN_DOCUMENT
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (code_task("T01"), True),
        (code_task("T01", path="src/Parser.PY"), True),
        (code_task("T01", path="README.md"), False),
        (code_task("T01", path="docs/guide.RST"), False),
        (code_task("T01", path="notes.txt"), False),
        ({**code_task("T01"), "effect": {"kind": "none"}}, False),
        ({**code_task("T01"), "outputs": [{"root": "workspace", "path": "artifacts/architecture.md"}]}, False),
        ({**code_task("T01"), "outputs": []}, False),
        ({"id": "T01", "effect": ["local_write"], "outputs": "x"}, False),
        ({"id": "T01", "effect": {"kind": "local_write"}, "outputs": [None, {"root": "target", "path": 7}]}, False),
        ("not a task", False),
        (None, False),
    ],
)
def test_a_code_task_writes_a_non_prose_file_into_the_target(task: object, expected: bool) -> None:
    assert lib.is_code_task(task) is expected


class TestDesignWarnings:
    def warnings(self, project: Path, *tasks: dict[str, Any], status: str = "EXECUTING") -> list[str]:
        return lib.design_document_warnings(project, status, new_state(*tasks))

    def test_a_missing_document_is_named_for_a_code_project(self, project: Path) -> None:
        found = self.warnings(project, code_task("T01"))
        assert len(found) == 1
        assert found[0].startswith("artifacts/architecture.md is missing")
        assert "design task" in found[0]

    @pytest.mark.parametrize("status", ["PLANNING", "EXECUTING", "REVIEW"])
    def test_the_working_statuses_are_checked(self, project: Path, status: str) -> None:
        assert self.warnings(project, code_task("T01"), status=status)

    @pytest.mark.parametrize("status", ["ALIGNING", "BLOCKED", "DONE", "CANCELLED"])
    def test_every_other_status_is_quiet(self, project: Path, status: str) -> None:
        assert self.warnings(project, code_task("T01"), status=status) == []

    def test_unusable_status_values_give_no_finding_and_no_crash(self, project: Path) -> None:
        for status in (None, ["EXECUTING"], {"a": 1}, 7):
            assert lib.design_document_warnings(project, status, new_state(code_task("T01"))) == []

    def test_a_docs_only_plan_is_quiet(self, project: Path) -> None:
        assert self.warnings(project, docs_task("T01"), docs_task("T02")) == []
        assert self.warnings(project) == []

    def test_a_skipped_code_task_does_not_make_a_code_project(self, project: Path) -> None:
        assert self.warnings(project, code_task("T01", "SKIPPED"), docs_task("T02")) == []

    def test_a_project_created_before_the_split_is_quiet(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "PLANNING_SPLIT_FROM", FUTURE)
        assert self.warnings(project, code_task("T01")) == []
        assert lib.design_document_warnings(project, "EXECUTING", {"tasks": [code_task("T01")]}) == []

    def test_a_complete_document_with_every_row_is_quiet(self, project: Path) -> None:
        write_design(project, FULL_DESIGN)
        assert self.warnings(project, code_task("T01", "TODO"), code_task("T02", "DONE")) == []

    def test_a_done_code_task_without_a_status_row_is_named(self, project: Path) -> None:
        write_design(project, FULL_DESIGN)
        found = self.warnings(project, code_task("T02", "DONE"), code_task("T03", "DONE"), code_task("T04", "TODO"))
        assert found == ["artifacts/architecture.md has no 'Implementation status' row for DONE code task(s): T03"]

    def test_a_row_must_be_a_table_row_and_the_whole_id(self, project: Path) -> None:
        write_design(project, FULL_DESIGN + "\nT05 is mentioned in prose only.\n| T050 | other | done | none |\n")
        found = self.warnings(project, code_task("T05", "DONE"))
        assert found and "T05" in found[0]

    def test_a_missing_section_is_named_and_not_applicable_counts_as_present(self, project: Path) -> None:
        write_design(project, FULL_DESIGN.replace("## 5. Key flows\n\nNot applicable: test\n\n", ""))
        found = self.warnings(project, code_task("T02", "DONE"))
        assert len(found) == 1
        assert "lacks section(s): Key flows" in found[0]
        assert "Not applicable" in found[0]

    def test_a_profile_adds_the_sections_it_requires(self, project: Path) -> None:
        write_design(project, FULL_DESIGN.replace("Profiles: core", "Profiles: core, algorithmic, service"))
        found = self.warnings(project, code_task("T02", "DONE"))
        assert len(found) == 1
        for title in ("Algorithms", "Security, privacy and limits", "Operations and rollout"):
            assert title in found[0]
        assert "Concurrency" not in found[0]
        extra = "".join(f"## {title}\n\nNot applicable: test\n\n" for title in (
            "Algorithms", "Security, privacy and limits", "Operations and rollout"))
        write_design(project, FULL_DESIGN.replace("Profiles: core", "Profiles: core, algorithmic, service") + extra)
        assert self.warnings(project, code_task("T02", "DONE")) == []

    def test_an_unknown_profile_adds_nothing(self, project: Path) -> None:
        write_design(project, FULL_DESIGN.replace("Profiles: core", "Profiles: core, quantum"))
        assert self.warnings(project, code_task("T02", "DONE")) == []

    def test_a_directory_named_like_the_document_reads_as_missing(self, project: Path) -> None:
        (project / lib.DESIGN_DOCUMENT).mkdir(parents=True)
        assert self.warnings(project, code_task("T01"))[0].startswith("artifacts/architecture.md is missing")

    def test_a_section_heading_at_a_deeper_level_ends_at_the_next_peer(self) -> None:
        text = "# D\n\n## 13. Implementation status\n\n### Notes\n\n| T01 | x |\n\n## Appendix\n\n| T09 | y |\n"
        body = lib._design_section_text(text, "Implementation status")
        assert body is not None
        assert "T01" in body
        assert "T09" not in body


def test_validation_carries_the_warning_and_never_an_error(project: Path) -> None:
    update_project_data(
        project, {"status": "PLANNING", "tasks": [{**code_task("T01"), "name": "Design", "success_criteria": "done",
                                                    "verification": "check"}]}, expected_revision=0,
    )
    report = lib.validate_project(project)
    assert report.valid, report.errors
    assert any(warning.startswith("artifacts/architecture.md is missing") for warning in report.warnings)
    write_design(project, FULL_DESIGN)
    report = lib.validate_project(project)
    assert report.valid
    assert not [warning for warning in report.warnings if "artifacts/architecture.md" in warning]


# ---- the documents and the code agree -----------------------------------------------------------

READ_EDIT = re.compile(r"\b(?P<verb>read|edit) <project-dir> (?P<document>[a-z][a-z-]*)")
JSON_DOCUMENT = re.compile(r'"document"\s*:\s*"(?P<document>[A-Za-z][\w-]*)')
LEGACY_CONTEXT = re.compile(r"root\b|legacy|older|begun before|before the split|old name", re.IGNORECASE)
AGREEMENT_FILE = re.compile(r"(?<![\w/-])architecture\.md\b")
WORKFLOW_DOCUMENT_IDS = {"spec", "planning", "architecture", "reflection", "handoff", "memory-staging", "evidence",
                         "tasks", "reviews", "artifacts"}


def unknown_cli_documents(text: str) -> list[str]:
    """Ids after `read <project-dir>` or `edit <project-dir>` that the command line does not accept."""
    accepted = {
        "read": {"spec", "evidence", *manage_workspace.WHOLE_DOCUMENTS, "friction", "notes"},
        "edit": {"spec", *manage_workspace.WHOLE_DOCUMENTS},
    }
    return [
        f"{found['verb']} {found['document']}" for found in READ_EDIT.finditer(text)
        if found["document"] not in accepted[found["verb"]]
    ]


def unknown_workflow_documents(text: str) -> list[str]:
    return [found["document"] for found in JSON_DOCUMENT.finditer(text)
            if found["document"].split("/")[0] not in WORKFLOW_DOCUMENT_IDS]


def old_agreement_names(text: str) -> list[str]:
    """Lines that call a root `architecture.md` something other than a legacy file, and old links."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if "architecture-review" in line:
            found.append(f"{number}: links the renamed architecture-review reference")
        elif AGREEMENT_FILE.search(line) and not LEGACY_CONTEXT.search(line):
            found.append(f"{number}: {line.strip()[:80]}")
    return found


def shipped_documents() -> list[Path]:
    return [SKILL_DIR / "SKILL.md", SKILL_DIR / "README.md", *sorted((SKILL_DIR / "references").glob("*.md"))]


def test_the_sweep_finds_the_new_and_the_renamed_references() -> None:
    names = {path.name for path in shipped_documents()}
    assert {"planning-review.md", "design-document.md", "SKILL.md", "README.md"} <= names
    assert "architecture-review.md" not in names


def test_every_document_id_a_reference_names_is_one_the_tool_accepts() -> None:
    problems = []
    for path in shipped_documents():
        text = path.read_text(encoding="utf-8")
        problems += [f"{path.name}: {item}" for item in unknown_cli_documents(text)]
        problems += [f"{path.name}: {item}" for item in unknown_workflow_documents(text)]
    assert problems == []


def test_no_reference_calls_the_agreement_document_architecture_md() -> None:
    problems = []
    for path in shipped_documents():
        problems += [f"{path.name}:{item}" for item in old_agreement_names(path.read_text(encoding="utf-8"))]
    assert problems == []


def test_the_references_agree_with_the_code_on_the_ids() -> None:
    assert manage_workspace.WHOLE_DOCUMENTS == ("reflection", "planning", "architecture", "handoff")
    assert lib.PLANNING_FILENAME == "planning.md"
    assert lib.DESIGN_DOCUMENT == "artifacts/architecture.md"
    text = (SKILL_DIR / "references/design-document.md").read_text(encoding="utf-8")
    assert "`artifacts/architecture.md`" in text
    assert "`Template: v1`" in text
    # Every section the validator requires is a heading in the template the reference gives.
    table = "\n".join(line for line in text.splitlines() if line.startswith("|"))
    for title in (*lib.DESIGN_CORE_SECTIONS, *(t for titles in lib.DESIGN_PROFILE_SECTIONS.values() for t in titles)):
        assert f"| {title} |" in table, title
    for profile in lib.DESIGN_PROFILE_SECTIONS:
        assert f"`{profile}`" in text


def test_the_planning_rules_require_a_first_design_task() -> None:
    text = (SKILL_DIR / "references/planning-review.md").read_text(encoding="utf-8")
    assert "the first task is the design task" in text
    assert "`artifacts/architecture.md`" in text
    assert "its status row" in text
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "design-document.md" in skill
    assert "references/planning-review.md" in skill


class TestTheAgreementChecksHaveTeeth:
    def test_an_unknown_command_line_id_is_caught(self) -> None:
        assert unknown_cli_documents("Run `read <project-dir> design` first.") == ["read design"]
        assert unknown_cli_documents("`edit <project-dir> evidence`") == ["edit evidence"]
        assert unknown_cli_documents("`read <project-dir> planning` and `edit <project-dir> architecture`") == []

    def test_an_unknown_workflow_id_is_caught(self) -> None:
        assert unknown_workflow_documents('{"document":"design"}') == ["design"]
        assert unknown_workflow_documents('{"document": "tasks/T01"} {"document":"planning"}') == []

    def test_an_agreement_called_architecture_md_is_caught(self) -> None:
        assert old_agreement_names("Agree the plan in `architecture.md`.") == [
            "1: Agree the plan in `architecture.md`."
        ]
        assert old_agreement_names("The design is `artifacts/architecture.md`.") == []
        assert old_agreement_names("A legacy project keeps a root `architecture.md`.") == []

    def test_a_link_to_the_renamed_reference_is_caught(self) -> None:
        assert old_agreement_names("See [review](architecture-review.md).") == [
            "1: links the renamed architecture-review reference"
        ]

