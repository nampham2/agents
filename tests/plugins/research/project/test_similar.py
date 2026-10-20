"""The duplicate-project guard: scoring, the init refusal, the acknowledgment and word-based discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_lib import WorkspaceError, atomic_write_json
from workspace_session import list_projects
from workspace_similar import (
    ANYWHERE_SCORE,
    SAME_REPOSITORY_SCORE,
    check_new_project,
    find_similar,
    repository_of,
    similarity,
    tokens,
)


def _row(root: Path, name: str, title: str, status: str, working_directory: Path) -> Path:
    """A discovery row: the four fields `list_projects` reads, and nothing else."""
    directory = root / name
    directory.mkdir()
    atomic_write_json(
        directory / "project.json",
        {"project": name, "title": title, "status": status, "working_directory": str(working_directory)},
    )
    return directory


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


def test_tokens_drop_stop_words_short_words_and_one_suffix() -> None:
    assert tokens("Fix the flaky tests for wiki batches") == {"fix", "flaky", "test", "wiki", "batch"}
    assert tokens("To be or not to be") == frozenset()
    assert tokens("Parsing parsed parses parse") == {"pars", "parse"}
    assert tokens("") == frozenset()


def test_similarity_weighs_rare_words_above_common_ones() -> None:
    frequencies = {"common": 10, "rare": 1, "other1": 1, "other2": 1}
    sharing_common = similarity(
        frozenset({"common", "other1"}), frozenset({"common", "other2"}), frequencies, 10
    )
    sharing_rare = similarity(frozenset({"rare", "other1"}), frozenset({"rare", "other2"}), frequencies, 10)
    assert 0 < sharing_common < sharing_rare < 1


def test_similarity_edges() -> None:
    words = frozenset({"alpha", "bravo"})
    assert similarity(words, words, {"alpha": 1, "bravo": 1}, 1) == 1.0
    assert similarity(words, frozenset({"charlie"}), {}, 1) == 0.0
    assert similarity(frozenset(), words, {}, 1) == 0.0
    assert similarity(words, frozenset(), {}, 1) == 0.0


def test_a_worktree_path_maps_to_its_repository(tmp_path: Path) -> None:
    assert repository_of(str(tmp_path / "repo.worktrees" / "2026-10-20-003")) == str(tmp_path / "repo")
    assert repository_of(str(tmp_path / "repo")) == str(tmp_path / "repo")


def test_a_live_look_alike_in_the_same_repository_blocks(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    found = find_similar(root, "Cut wiki refresh cost per run", repository)
    assert [match["project"] for match in found["blocking"]] == ["2026-10-01-001"]
    assert found["blocking"][0]["same_repository"] is True
    assert found["related"] == []


def test_a_worktree_working_directory_still_counts_as_the_same_repository(root: Path, repository: Path) -> None:
    worktree = repository.parent / "repo.worktrees" / "2026-10-01-001"
    # Similarity 0.45: only the same-repository bar (0.35) lets it through, so the mapping decides.
    _row(root, "2026-10-01-001", "alpha bravo charlie delta echo", "EXECUTING", worktree)
    assert find_similar(root, "alpha bravo charlie foxtrot", repository)["blocking"]


def test_a_finished_look_alike_is_related_and_never_blocks(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "DONE", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent: cut cost per refresh", "CANCELLED", repository)
    found = find_similar(root, "Cut wiki refresh cost per run", repository)
    assert found["blocking"] == []
    assert [match["project"] for match in found["related"]] == ["2026-10-01-001", "2026-10-01-002"]
    assert check_new_project(root, "Cut wiki refresh cost per run", repository, []) != []


def test_an_unrelated_title_passes_without_a_warning(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    assert check_new_project(root, "Dashboard for bidding performance", repository, []) == []


def test_the_threshold_is_lower_in_the_same_repository_than_elsewhere(
    root: Path, repository: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    title = "alpha bravo charlie delta echo"
    _row(root, "2026-10-01-001", title, "EXECUTING", repository)
    _row(root, "2026-10-01-002", title, "EXECUTING", other)
    # Three of five words shared: similarity 0.6, above both bars.
    found = find_similar(root, "alpha bravo charlie", repository)
    assert {match["project"] for match in found["blocking"]} == {"2026-10-01-001", "2026-10-01-002"}
    # One new word: similarity 0.42, above the same-repository bar (0.35) and below the bar elsewhere (0.5).
    partial = find_similar(root, "alpha bravo charlie foxtrot", repository)
    assert [match["project"] for match in partial["blocking"]] == ["2026-10-01-001"]
    assert 0.35 <= partial["blocking"][0]["score"] < 0.5
    assert ANYWHERE_SCORE > SAME_REPOSITORY_SCORE


def test_unreadable_and_legacy_rows_are_no_evidence(root: Path, repository: Path) -> None:
    broken = root / "2026-10-01-001"
    broken.mkdir()
    (broken / "project.json").write_text("[]", encoding="utf-8")
    legacy = root / "2026-10-01-002"
    legacy.mkdir()
    (legacy / "00_meta.yaml").touch()
    (legacy / "02_task_plan.md").touch()
    _row(root, "2026-10-01-003", "Wiki refresh agent: cut cost per refresh", "PARKED", repository)
    assert find_similar(root, "Cut wiki refresh cost per run", repository) == {"blocking": [], "related": []}


def test_every_page_of_projects_is_scored(root: Path, repository: Path) -> None:
    for index in range(105):
        _row(root, f"2026-09-01-{index:03}", f"Unrelated topic number {index} zzz{index}", "DONE", repository)
    _row(root, "2026-08-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    found = find_similar(root, "Cut wiki refresh cost per run", repository)
    assert [match["project"] for match in found["blocking"]] == ["2026-08-01-001"]


def test_best_match_comes_first(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent cost", "EXECUTING", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent cost cut per run", "EXECUTING", repository)
    found = find_similar(root, "Cut wiki refresh agent cost per run", repository)
    assert [match["score"] for match in found["blocking"]] == sorted(
        (match["score"] for match in found["blocking"]), reverse=True
    )
    assert len(found["blocking"]) == 2


def test_check_refuses_naming_each_live_project_and_the_flag(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    with pytest.raises(WorkspaceError) as refusal:
        check_new_project(root, "Cut wiki refresh cost per run", repository, [])
    message = str(refusal.value)
    assert "created nothing" in message
    assert "2026-10-01-001 EXECUTING" in message
    assert "ask whether" in message
    assert "--acknowledge-duplicates 2026-10-01-001" in message


def test_check_accepts_the_acknowledged_ids_and_lists_finished_ones(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent: cut cost per refresh", "DONE", repository)
    warnings = check_new_project(root, "Cut wiki refresh cost per run", repository, ["2026-10-01-001"])
    assert len(warnings) == 1
    assert "2026-10-01-002" in warnings[0]


def test_check_refuses_an_acknowledgment_that_is_not_a_blocking_match(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent: cut cost per refresh", "DONE", repository)
    with pytest.raises(WorkspaceError, match=r"2026-10-01-002.*not a live project"):
        check_new_project(root, "Cut wiki refresh cost per run", repository, ["2026-10-01-001", "2026-10-01-002"])
    with pytest.raises(WorkspaceError, match=r"2026-09-09-009.*not a live project"):
        check_new_project(root, "Dashboard for bidding performance", repository, ["2026-09-09-009"])


def test_check_with_a_missing_root_has_nothing_to_compare(tmp_path: Path, repository: Path) -> None:
    assert check_new_project(tmp_path / "absent", "Anything at all", repository, []) == []


def test_init_creates_nothing_when_a_live_project_looks_alike(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    before = sorted(path.name for path in root.iterdir())
    code = _manage(["init", root, "--title", "Cut wiki refresh cost per run", "--working-directory", repository])
    assert code != 0
    assert sorted(path.name for path in root.iterdir()) == before
    assert "2026-10-01-001" in capsys.readouterr().err


def test_init_proceeds_after_the_acknowledgment_and_records_it(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent: cut cost per refresh", "DONE", repository)
    code = _manage([
        "init", root, "--title", "Cut wiki refresh cost per run", "--working-directory", repository,
        "--acknowledge-duplicates", "2026-10-01-001", "--json",
    ])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    spec = (Path(result["project_directory"]) / "spec.md").read_text(encoding="utf-8")
    assert "acknowledged these live projects as possible duplicates at init: 2026-10-01-001." in spec
    assert any("2026-10-01-002" in warning for warning in result["warnings"])


def test_init_of_an_unrelated_title_needs_no_acknowledgment(
    root: Path, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    code = _manage(["init", root, "--title", "Dashboard for bidding", "--working-directory", repository, "--json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert "acknowledged" not in (Path(result["project_directory"]) / "spec.md").read_text(encoding="utf-8")


def test_init_with_a_new_root_has_nothing_to_compare(tmp_path: Path, repository: Path) -> None:
    root = tmp_path / "fresh"
    assert _manage(["init", root, "--create-root", "--title", "First", "--working-directory", repository]) == 0


def test_find_similar_prints_both_lists(root: Path, repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _row(root, "2026-10-01-001", "Wiki refresh agent: cut cost per refresh", "EXECUTING", repository)
    code = _manage([
        "find-similar", root, "--title", "Cut wiki refresh cost per run", "--working-directory", repository,
    ])
    assert code == 0
    found = json.loads(capsys.readouterr().out)
    assert [match["project"] for match in found["blocking"]] == ["2026-10-01-001"]
    assert found["related"] == []


def test_a_multi_word_query_finds_a_project_when_every_word_occurs(root: Path, repository: Path) -> None:
    _row(root, "2026-10-01-001", "Research retro skill: iterative improvement of the plugin", "DONE", repository)
    _row(root, "2026-10-01-002", "Wiki refresh agent", "DONE", repository)
    # The old behavior looked for the whole phrase as one substring and found nothing.
    assert list_projects(root, query="plugin research skill")["total"] == 1
    assert list_projects(root, query="research retro")["total"] == 1
    assert list_projects(root, query="plugin wiki")["total"] == 0
    assert list_projects(root, query="  ")["total"] == 2
    assert list_projects(root, query="WIKI")["total"] == 1
