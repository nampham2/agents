"""c10: `readiness` compares required check names with the job names the CI files report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from workspace_ci import GITLAB_RESERVED_KEYS, ci_checks
from workspace_lib import WorkspaceError
from workspace_workflows import workflow

from tests.plugins.research.project.test_worktrees import add_worktree, make_project, make_repository, record

REPO = Path(__file__).resolve().parents[4]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_copy_of_ci_yml_reports_its_jobs_and_no_step_or_workflow_name(tmp_path: Path) -> None:
    write(tmp_path, ".github/workflows/ci.yml", CI_YML.read_text(encoding="utf-8"))
    result = ci_checks(tmp_path, ["test", "python-floor", "CI", "Install dependencies", "lint"])
    assert result["jobs"] == ["test", "python-floor"]
    assert result["matched"] == ["test", "python-floor"]
    assert result["missing"] == ["CI", "Install dependencies", "lint"]
    assert result["files"] == [".github/workflows/ci.yml"]


def test_a_job_name_overrides_its_key_and_yaml_files_are_read(tmp_path: Path) -> None:
    text = 'name: W\njobs:\n  # c\n  build:\n    name: "Build it"  # x\n    runs-on: x\n'
    write(tmp_path, ".github/workflows/a.yaml", text)
    result = ci_checks(tmp_path, ["Build it", "build"])
    assert result["jobs"] == ["Build it"] and result["missing"] == ["build"]


def test_a_matrix_expression_or_reusable_job_is_unreportable_and_noted(tmp_path: Path) -> None:
    write(
        tmp_path,
        ".github/workflows/m.yml",
        "jobs:\n  m:\n    strategy:\n      matrix:\n        v: [1]\n    runs-on: x\n"
        "  e:\n    name: t-${{ matrix.v }}\n  r:\n    uses: ./x.yml\n  ok:\n    runs-on: x\n",
    )
    result = ci_checks(tmp_path, ["m", "ok"])
    assert result["unreportable"] == ["m", "t-${{ matrix.v }}", "r"]
    assert result["jobs"] == ["ok"] and result["missing"] == ["m"]
    assert any("matrix" in note for note in result["notes"])


def test_a_tab_or_unreadable_file_is_skipped_and_reported(tmp_path: Path) -> None:
    write(tmp_path, ".github/workflows/t.yml", "jobs:\n\tx:\n")
    (tmp_path / ".github/workflows/b.yml").write_bytes(b"\xff\xfe")
    result = ci_checks(tmp_path, ["x"])
    assert {item["file"] for item in result["skipped"]} == {".github/workflows/t.yml", ".github/workflows/b.yml"}
    assert result["missing"] == ["x"] and result["files"] == []


def test_a_workflow_without_jobs_or_with_an_empty_block_has_none(tmp_path: Path) -> None:
    write(tmp_path, ".github/workflows/n.yml", "name: x\n")
    write(tmp_path, ".github/workflows/e.yml", "jobs:\n# only a comment\nname: y\n")
    assert ci_checks(tmp_path, [])["jobs"] == []


def test_gitlab_keys_minus_reserved_ones_are_jobs_and_include_is_noted(tmp_path: Path) -> None:
    write(
        tmp_path,
        ".gitlab-ci.yml",
        "include: x.yml\nstages: [a]\n.hidden:\n  a: 1\nlint:\n  script: x\n  stage: a\n# c\ntest: {}\n- stray\n",
    )
    result = ci_checks(tmp_path, ["lint", "stages"])
    assert result["jobs"] == ["lint", "test"] and result["missing"] == ["stages"]
    assert any("include" in note for note in result["notes"])
    assert "workflow" in GITLAB_RESERVED_KEYS


def test_omitted_requirements_no_files_and_no_root_say_so(tmp_path: Path) -> None:
    assert ci_checks(tmp_path, None)["required"] is None
    assert any("not supplied" in n for n in ci_checks(tmp_path, None)["notes"])
    assert any("no CI file" in n for n in ci_checks(tmp_path, ["x"])["notes"])
    assert any("no active" in n for n in ci_checks(None, ["x"], reason="no active target")["notes"])
    assert ci_checks(None, ["x"])["root"] is None


def test_quoted_keys_and_odd_lines_do_not_crash(tmp_path: Path) -> None:
    write(tmp_path, ".github/workflows/q.yml", "jobs:\n  'a b':\n    x\n    name: ''\n  - z\n")
    assert ci_checks(tmp_path, ["a b"])["jobs"] == ["a b"]
    write(tmp_path, ".github/workflows/u.yml", "jobs:\n  u:\n    name: 'open\n")
    assert ci_checks(tmp_path, ["open"])["matched"] == ["open"]


class TestReadinessChecks:
    def test_checks_read_the_recorded_worktree_not_the_base(self, tmp_path: Path, repository: Path, linked: Path
    ) -> None:
        write(linked, ".github/workflows/ci.yml", "jobs:\n  test:\n    runs-on: x\n")
        write(repository, ".github/workflows/ci.yml", "jobs:\n  other:\n    runs-on: x\n")
        project = make_project(tmp_path, repository)
        record(project, linked)
        result = workflow(project, "readiness", {"required_checks": ["test", "other"]})
        assert result["checks"]["matched"] == ["test"] and result["checks"]["missing"] == ["other"]
        assert result["checks"]["root"] == str(linked)

    def test_checks_is_always_present_and_valid_is_unchanged(self, tmp_path: Path, repository: Path, linked: Path
    ) -> None:
        project = make_project(tmp_path, repository)
        plain = workflow(project, "readiness", {})
        assert plain["checks"]["required"] is None and "no active" in " ".join(plain["checks"]["notes"])
        record(project, linked)
        bare = workflow(project, "readiness", {})
        with_checks = workflow(project, "readiness", {"required_checks": ["nope"]})
        assert with_checks["valid"] == bare["valid"] and with_checks["groups"] == bare["groups"]

    @pytest.mark.parametrize("bad", ["x", [1], [""], {"a": 1}])
    def test_required_checks_must_be_a_list_of_names(self, tmp_path: Path, repository: Path, bad: Any) -> None:
        with pytest.raises(WorkspaceError, match="required_checks"):
            workflow(make_project(tmp_path, repository), "readiness", {"required_checks": bad})

    def test_unknown_fields_are_still_refused_and_schema_prints_the_key(self, tmp_path: Path, repository: Path) -> None:
        from workspace_workflows import input_schema

        with pytest.raises(WorkspaceError):
            workflow(make_project(tmp_path, repository), "readiness", {"nope": 1})
        assert "required_checks" in input_schema("readiness")["allowed"]

    def test_a_malformed_worktree_entry_gives_no_root(self) -> None:
        from workspace_workflows import readiness_checks

        for items in ("x", ["x"], [{"role": "target", "status": "active", "kind": "none", "path": "/a"}]):
            assert readiness_checks({"worktrees": items}, ["x"])["root"] is None
        assert readiness_checks({}, None)["root"] is None

    def test_a_gone_directory_says_so_instead_of_reporting_no_ci_file(self, tmp_path: Path) -> None:
        from workspace_workflows import readiness_checks

        gone = tmp_path / "gone"
        entry = {"role": "target", "status": "active", "kind": "created", "path": str(gone)}
        result = readiness_checks({"worktrees": [entry]}, ["x"])
        assert result["root"] is None and result["missing"] == []
        assert any("no longer exists" in note for note in result["notes"])
