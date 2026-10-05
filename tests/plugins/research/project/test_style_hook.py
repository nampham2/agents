"""The advisory style findings as a user meets them: through validation, never through the module.

These tests import no style code. They call `validate_project` and the launchers, so a build
without the feature fails each of them on an assertion about a missing warning and not on an
import error: a failure that names the right reason is the only kind that shows a test discriminates.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_session import update_project_data

from tests.conftest import REPO_ROOT, backdate_project
from tests.plugins.research.project.test_workflow_automation import fill_spec, task

LONG = " ".join(["word"] * 30) + " end."  # 31 words: over the limit of 25
SHORT = "This sentence is short."
PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A new project (created after a cutoff moved into the past) that is still ALIGNING."""
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", PAST, raising=False)
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Style", working_directory=target, create_root=True)


def style(project: Path) -> list[str]:
    return [warning for warning in lib.validate_project(project).warnings if "form only" in warning]


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_a_long_sentence_in_the_architecture_gets_one_advisory_warning(project: Path) -> None:
    assert state_of(project)["status"] == "ALIGNING"  # the check does not wait for PLANNING
    errors_before = lib.validate_project(project).errors
    (project / "architecture.md").write_text("# A\n\n" + LONG + "\n", encoding="utf-8")
    assert style(project) == [
        "architecture.md (form only, never an error): L3 [length] sentence of 31 words (limit 25)"
    ]
    assert lib.validate_project(project).errors == errors_before  # advisory: validity does not change


def test_clean_records_get_no_style_warning(project: Path) -> None:
    (project / "architecture.md").write_text("# A\n\n" + SHORT + "\n", encoding="utf-8")
    (project / "handoff.md").write_text("# H\n\n" + SHORT + "\n", encoding="utf-8")
    assert style(project) == []


def test_a_project_from_before_the_cutoff_is_not_checked(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "architecture.md").write_text("# A\n\n" + LONG + "\n", encoding="utf-8")
    assert style(project)
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", FUTURE, raising=False)
    assert style(project) == []


def test_only_the_current_specification_is_read_and_lines_are_file_lines(project: Path) -> None:
    (project / "spec.md").write_text(
        "# T\n\n## Current specification\n\n### Objective and audience\n\n" + LONG + "\n\n"
        "## Decision history\n\n" + LONG + "\n",
        encoding="utf-8",
    )
    assert style(project) == ["spec.md (form only, never an error): L7 [length] sentence of 31 words (limit 25)"]


def test_numbered_items_use_the_step_limit_only_in_the_handoff(project: Path) -> None:
    item = "1. " + " ".join(["word"] * 22) + ".\n"  # 22 words: over 20, under 25
    (project / "architecture.md").write_text("# A\n\n" + item, encoding="utf-8")
    (project / "spec.md").write_text("# T\n\n## Current specification\n\n### Scope\n\n" + item, encoding="utf-8")
    assert style(project) == []
    (project / "handoff.md").write_text("# H\n\n" + item, encoding="utf-8")
    assert style(project) == ["handoff.md (form only, never an error): L3 [length] sentence of 22 words (limit 20)"]


def test_the_handoff_is_checked(project: Path) -> None:
    (project / "handoff.md").write_text("# H\n\n" + LONG + "\n", encoding="utf-8")
    assert [warning.split(" ")[0] for warning in style(project)] == ["handoff.md"]


def test_task_fields_are_checked_with_the_task_id(project: Path) -> None:
    fill_spec(project)
    bad = task("T01")
    bad["verification"] = " ".join(["Run"] * 21) + "."
    update_project_data(project, {"status": "PLANNING", "tasks": [bad]}, expected_revision=0)
    assert style(project) == [
        "task fields (form only, never an error): T01 verification [length] sentence of 21 words (limit 20)"
    ]


def test_task_fields_of_the_wrong_type_raise_nothing(project: Path) -> None:
    state = state_of(project)
    state["tasks"] = [{"id": "T01", "name": 5, "success_criteria": None, "verification": ["x"]}, "x", 3]
    report = lib.validate_v4_state(state, project)
    assert [warning for warning in report.warnings if "form only" in warning] == []
    assert report.errors  # the malformed tasks are the validator's to refuse, not the style check's


def test_unreadable_files_are_skipped_without_error(project: Path) -> None:
    (project / "architecture.md").write_bytes(b"\xff\xfe not utf-8 \x80")
    (project / "handoff.md").write_text("# H\n\n" + LONG + "\n", encoding="utf-8")
    assert [warning.split(" ")[0] for warning in style(project)] == ["handoff.md"]


def test_schema_3_has_no_style_check(project: Path) -> None:
    (project / "architecture.md").write_text("# A\n\n" + LONG + "\n", encoding="utf-8")
    report = lib.validate_v3_state(state_of(project), project)
    assert [warning for warning in report.warnings if "form only" in warning] == []


@pytest.mark.parametrize("created", [None, "", "not a date", 7, "2027-01-01T00:00:00", "2027-01-01"])
def test_a_created_stamp_that_cannot_be_judged_is_not_checked(created: object) -> None:
    assert lib.is_style_checked({"created": created}) is False


def test_the_cutoff_decides_by_instant_and_a_bad_cutoff_checks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "2026-10-15T00:00:00+00:00", raising=False)
    assert lib.is_style_checked({"created": "2026-10-15T02:00:00+02:00"}) is True
    assert lib.is_style_checked({"created": "2026-10-15T01:59:59+02:00"}) is False
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "someday", raising=False)
    assert lib.is_style_checked({"created": "2030-01-01T00:00:00+00:00"}) is False
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "2026-10-15T00:00:00", raising=False)  # naive cutoff
    assert lib.is_style_checked({"created": "2030-01-01T00:00:00+00:00"}) is False


@pytest.fixture(
    params=[
        REPO_ROOT / "plugins/research/bin/research-validate",
        REPO_ROOT / "plugins/research/skills/project/scripts/research-validate",
    ],
    ids=["claude-bin", "codex-skill-local"],
)
def validator(request: pytest.FixtureRequest) -> Path:
    return Path(request.param)


def test_both_launchers_print_the_same_warning(project: Path, validator: Path) -> None:
    """A launcher subprocess reads the production cutoff, so the project is made new on disk."""
    (project / "architecture.md").write_text("# A\n\n" + LONG + "\n", encoding="utf-8")
    backdate_project(project, created=FUTURE)
    result = subprocess.run([str(validator), str(project)], text=True, capture_output=True)
    lines = [line for line in result.stderr.splitlines() if "form only" in line]
    assert result.returncode == 0, result.stderr
    assert lines == [
        "WARNING: architecture.md (form only, never an error): L3 [length] sentence of 31 words (limit 25)"
    ]


@pytest.fixture(
    params=[
        REPO_ROOT / "plugins/research/bin/research-project",
        REPO_ROOT / "plugins/research/skills/project/scripts/research-project",
    ],
    ids=["claude-bin", "codex-skill-local"],
)
def manager(request: pytest.FixtureRequest) -> Path:
    return Path(request.param)


def test_context_validate_carries_the_same_warning_through_both_launchers(project: Path, manager: Path) -> None:
    (project / "architecture.md").write_text("# A\n\n" + LONG + "\n", encoding="utf-8")
    backdate_project(project, created=FUTURE)
    result = subprocess.run([str(manager), "context", str(project), "--validate"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    warnings = [w for w in json.loads(result.stdout)["validation"]["warnings"] if "form only" in w]
    assert warnings == ["architecture.md (form only, never an error): L3 [length] sentence of 31 words (limit 25)"]
