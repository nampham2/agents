"""The tag commands through `main()`: init and start --create, suggest-tags and tag."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_lib import allocate_project, load_json


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    return tmp_path / "workspace"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def _manage(arguments: list[Any]) -> int:
    with patch.object(sys, "argv", ["manage", *[str(argument) for argument in arguments]]):
        return manage_workspace.main()


def _names(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.is_dir() and path.name[:2] == "20")


def _init(root: Path, repository: Path, *extra: str, title: str = "Brand new work") -> int:
    return _manage(["init", root, "--title", title, "--working-directory", repository, *extra])


# --- init ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, fragment",
    [
        ([], "at least 1"),
        (["a", "b", "c", "d", "e", "f"], "at most 5"),
        (["same", "Same"], "duplicate"),
        (["Bad!"], "Bad!"),
    ],
)
def test_init_refuses_bad_tags_and_creates_nothing(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str], tags: list[str], fragment: str
) -> None:
    arguments = [part for tag in tags for part in ("--tag", tag)]
    code = _init(root, repository, *arguments)
    assert code != 0
    assert _names(root) == []
    error = capsys.readouterr().err
    assert "init created nothing" in error and fragment in error


def test_init_records_normalized_tags_and_prints_them(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(root, repository, "--tag", "My Tag", "--tag", "dashboard", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tags"] == ["my-tag", "dashboard"]
    assert load_json(Path(result["project_directory"]) / "project.json")["tags"] == ["my-tag", "dashboard"]


def test_init_checks_tags_before_the_duplicate_check(root: Path, repository: Path) -> None:
    allocate_project(root, title="Wiki refresh agent cost cut", working_directory=repository)
    before = _names(root)
    assert _init(root, repository, title="Wiki refresh agent cost reduction") != 0
    assert _names(root) == before


# --- start ---------------------------------------------------------------------------------------


def _start(root: Path, repository: Path, *extra: str, title: str = "Brand new work") -> int:
    return _manage(["start", "--workspace-root", root, "--title", title, "--working-directory", repository, *extra])


def test_start_create_needs_tags_and_creates_nothing_without_them(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _start(root, repository, "--create") != 0
    assert _names(root) == []
    assert "init created nothing" in capsys.readouterr().err


def test_start_create_with_tags_creates_the_project(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _start(root, repository, "--create", "--tag", "alpha") == 0
    capsys.readouterr()
    [name] = _names(root)
    assert load_json(root / name / "project.json")["tags"] == ["alpha"]


def test_start_without_create_and_a_resume_ignore_tags(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _start(root, repository, "--tag", "alpha") == 0
    assert json.loads(capsys.readouterr().out) == {"candidates": []}
    assert _names(root) == []
    project = allocate_project(root, title="Brand new work", working_directory=repository, tags=["kept"])
    assert _start(root, repository, "--create", "--tag", "ignored") == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["matched"]["project"] == project.name
    assert load_json(project / "project.json")["tags"] == ["kept"]
    assert _names(root) == [project.name]


# --- suggest-tags --------------------------------------------------------------------------------


def test_suggest_tags_prints_json(root: Path, repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    allocate_project(root, title="Older job", working_directory=repository, tags=["bigquery"])
    assert _manage(["suggest-tags", root, "--title", "Nightly report", "--working-directory", repository]) == 0
    suggestions = json.loads(capsys.readouterr().out)["suggestions"]
    assert suggestions[0]["tag"] == "bigquery" and suggestions[0]["source"] == "repository"
    assert len(suggestions[0]["projects"]) == 1
    assert {item["source"] for item in suggestions} == {"repository", "title-word"}


def test_suggest_tags_refuses_a_bad_limit(root: Path, repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _manage(["suggest-tags", root, "--title", "x y z", "--working-directory", repository, "--limit", "51"])
    assert code != 0 and "limit" in capsys.readouterr().err


# --- tag -----------------------------------------------------------------------------------------


def test_tag_add_and_remove_print_the_result(root: Path, repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = allocate_project(root, title="Tagged", working_directory=repository, tags=["alpha"])
    assert _manage(["tag", project, "add", "Beta", "--expected-revision", 0, "--json"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added == {
        "operation": "tag", "project_directory": str(project.resolve()), "revision": 1, "tags": ["alpha", "beta"],
    }
    assert _manage(["tag", project, "remove", "alpha", "--expected-revision", 1]) == 0
    assert capsys.readouterr().out.strip() == "Committed revision 2: tags beta"


def test_tag_refusals_exit_nonzero_with_the_prefix(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = allocate_project(root, title="Tagged", working_directory=repository, tags=["alpha"])
    assert _manage(["tag", project, "remove", "alpha", "--expected-revision", 0]) != 0
    assert "tag refused:" in capsys.readouterr().err
    assert load_json(project / "project.json")["tags"] == ["alpha"]
