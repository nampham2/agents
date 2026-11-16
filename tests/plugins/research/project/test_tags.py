"""Project tags: the format, validation, creation, list rows, suggestions and the `tag` command."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from workspace_lib import (
    MAX_TAGS,
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    atomic_write_json,
    load_json,
    normalize_tag,
    normalize_tags,
    tag_project,
    tags_of,
    validate_v4_state,
)
from workspace_operations import init_project
from workspace_session import list_projects
from workspace_similar import find_similar, suggest_tags


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    return tmp_path / "workspace"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def _project(root: Path, repository: Path, title: str = "A project", **options: Any) -> Path:
    return allocate_project(root, title=title, working_directory=repository, **options)


def _state(project: Path) -> dict[str, Any]:
    return load_json(project / "project.json")


def _rewrite(project: Path, **changes: Any) -> None:
    state = _state(project)
    state.update(changes)
    atomic_write_json(project / "project.json", state)


# --- normalization -------------------------------------------------------------------------------


def test_normalize_tag_lowercases_and_joins_with_hyphens() -> None:
    assert normalize_tag("  My Tag ") == "my-tag"
    assert normalize_tag("snake_case__name") == "snake-case-name"
    assert normalize_tag("--Edge--") == "edge"
    assert normalize_tag("a   b") == "a-b"
    assert normalize_tag("v2") == "v2"


@pytest.mark.parametrize("text", ["", "   ", "---", "naïve", "tag!", "a" * 25, "a/b", "emoji🙂"])
def test_normalize_tag_rejects_what_still_fails(text: str) -> None:
    with pytest.raises(WorkspaceError):
        normalize_tag(text)


def test_normalize_tag_refuses_a_value_that_is_not_text() -> None:
    with pytest.raises(WorkspaceError, match="must be text"):
        not_text: Any = 3
        normalize_tag(not_text)


def test_normalize_tag_accepts_the_longest_tag() -> None:
    assert normalize_tag("a" * 24) == "a" * 24


def test_normalize_tags_keeps_input_order_and_rejects_duplicates_after_normalizing() -> None:
    assert normalize_tags(["Zeta", "alpha"]) == ["zeta", "alpha"]
    with pytest.raises(WorkspaceError, match="duplicate"):
        normalize_tags(["My Tag", "my-tag"])


def test_normalize_tags_enforces_the_maximum_and_the_minimum() -> None:
    assert len(normalize_tags([f"t{i}" for i in range(MAX_TAGS)])) == MAX_TAGS
    with pytest.raises(WorkspaceError, match="at most 5"):
        normalize_tags([f"t{i}" for i in range(MAX_TAGS + 1)])
    assert normalize_tags([]) == []
    with pytest.raises(WorkspaceError, match="at least 1"):
        normalize_tags([], minimum=1)


def test_tags_of_reads_leniently() -> None:
    assert tags_of({"tags": ["a", "b-c"]}) == ["a", "b-c"]
    assert tags_of({}) == []
    assert tags_of({"tags": "not a list"}) == []
    assert tags_of({"tags": ["ok", 3]}) == []
    assert tags_of({"tags": ["Bad Tag"]}) == []
    assert tags_of({"tags": ["a", "a"]}) == []
    assert tags_of({"tags": [f"t{i}" for i in range(6)]}) == []


# --- validation ----------------------------------------------------------------------------------


def _findings(project: Path) -> list[str]:
    return validate_v4_state(_state(project), project, check_files=False).errors


def test_a_project_without_tags_is_valid(root: Path, repository: Path) -> None:
    assert _findings(_project(root, repository)) == []


def test_a_project_with_one_to_five_valid_tags_is_valid(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha", "beta"])
    assert _state(project)["tags"] == ["alpha", "beta"]
    assert _findings(project) == []


def test_the_empty_tag_list_is_valid(root: Path, repository: Path) -> None:
    project = _project(root, repository)
    _rewrite(project, tags=[])
    assert _findings(project) == []


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("not a list", "list"),
        ([1], "string"),
        (["Bad Tag"], "bad-tag"),
        (["a", "a"], "duplicate"),
        ([f"t{i}" for i in range(6)], "at most 5"),
        (["a" * 25], "24"),
    ],
)
def test_malformed_tags_are_findings_not_exceptions(root: Path, repository: Path, value: Any, fragment: str) -> None:
    project = _project(root, repository)
    _rewrite(project, tags=value)
    findings = _findings(project)
    assert findings and any(fragment in finding for finding in findings), findings


def test_a_tag_that_cannot_be_repaired_is_still_a_finding_without_a_hint(root: Path, repository: Path) -> None:
    project = _project(root, repository)
    _rewrite(project, tags=["!!!", "Fixable Tag"])
    findings = _findings(project)
    assert any("'!!!'" in finding and "write it as" not in finding for finding in findings)
    assert any("write it as 'fixable-tag'" in finding for finding in findings)


def test_a_v3_project_still_rejects_the_tags_field(root: Path, repository: Path) -> None:
    from workspace_lib import validate_v3_state

    state = copy.deepcopy(_state(_project(root, repository)))
    state["schema_version"] = 3
    del state["execution"]
    state["tags"] = ["a"]
    assert any("tags" in error for error in validate_v3_state(state, root, check_files=False).errors)


# --- creation ------------------------------------------------------------------------------------


def test_allocate_project_normalizes_before_it_creates_a_directory(root: Path, repository: Path) -> None:
    before = sorted(path.name for path in root.iterdir())
    with pytest.raises(WorkspaceError):
        _project(root, repository, tags=["ok", "Bad Tag!"])
    assert sorted(path.name for path in root.iterdir()) == before


@pytest.mark.parametrize("title", ["two\nlines", "x\n\n<!-- research-entry: a -->", "tail\r\n## Heading", "a\u2028b"])
def test_a_title_must_be_one_line_and_nothing_is_created(root: Path, repository: Path, title: str) -> None:
    before = sorted(path.name for path in root.iterdir())
    with pytest.raises(WorkspaceError, match="one line"):
        _project(root, repository, title)
    assert sorted(path.name for path in root.iterdir()) == before


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_a_blank_title_is_refused_and_nothing_is_created(root: Path, repository: Path, title: str) -> None:
    before = sorted(path.name for path in root.iterdir())
    with pytest.raises(WorkspaceError, match="non-empty"):
        _project(root, repository, title)
    assert sorted(path.name for path in root.iterdir()) == before


@pytest.mark.parametrize("title", ["x\n", "x\u2028", "  x  "])
def test_creation_strips_surrounding_line_breaks_and_spaces(root: Path, repository: Path, title: str) -> None:
    assert _state(_project(root, repository, title))["title"] == "x"


@pytest.mark.parametrize("title", ["x <!-- research-proposal: kind=requirements --> y", "plain <!-- note"])
def test_a_title_cannot_hold_a_record_marker_on_one_line(root: Path, repository: Path, title: str) -> None:
    before = sorted(path.name for path in root.iterdir())
    with pytest.raises(WorkspaceError, match="marker"):
        _project(root, repository, title)
    assert sorted(path.name for path in root.iterdir()) == before


def test_allocate_project_stores_the_normalized_tags(root: Path, repository: Path) -> None:
    assert _state(_project(root, repository, tags=["Research Plugin"]))["tags"] == ["research-plugin"]


def test_init_project_records_given_tags(root: Path, repository: Path) -> None:
    project, _ = init_project(root, "Tagged project", repository, tags=["dashboard"])
    assert _state(project)["tags"] == ["dashboard"]


# --- list rows -----------------------------------------------------------------------------------


def test_list_rows_carry_tags_and_predecessor_outside_the_query_match(root: Path, repository: Path) -> None:
    tagged = _project(root, repository, "Tagged", tags=["alpha"])
    _rewrite(tagged, predecessor="2026-01-01-001")
    plain = _project(root, repository, "Plain")
    rows = {row["project"]: row for row in list_projects(root, limit=100)["projects"]}
    assert rows[tagged.name]["tags"] == ["alpha"]
    assert rows[tagged.name]["predecessor"] == "2026-01-01-001"
    assert rows[plain.name]["tags"] == []
    assert "predecessor" not in rows[plain.name]
    # A search for a tag name must not match through the row values, and a list must not break the slicing.
    assert list_projects(root, query="alpha")["total"] == 0


def test_list_rows_stay_readable_with_malformed_tags_and_invalid_rows(root: Path, repository: Path) -> None:
    broken = _project(root, repository, "Broken tags")
    _rewrite(broken, tags="oops")
    (root / "2026-01-01-009").mkdir()
    (root / "2026-01-01-009" / "project.json").write_text("{not json", encoding="utf-8")
    rows = {row["project"]: row for row in list_projects(root, limit=100)["projects"]}
    assert rows[broken.name]["tags"] == []
    assert rows["2026-01-01-009"]["status"] == "INVALID"
    assert rows["2026-01-01-009"]["tags"] == []


def test_find_similar_matches_carry_tags(root: Path, repository: Path) -> None:
    _project(root, repository, "Wiki refresh agent cost cut", tags=["wiki"])
    found = find_similar(root, "Wiki refresh agent cost reduction", repository)
    matches = found["blocking"] + found["related"]
    assert matches and matches[0]["tags"] == ["wiki"]


# --- suggestions ---------------------------------------------------------------------------------


def test_suggestions_rank_same_repository_then_similar_titles_then_title_words(
    root: Path, tmp_path: Path, repository: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _project(root, repository, "Unrelated nightly report", tags=["reporting", "bigquery"])
    _project(root, repository, "Another nightly job", tags=["bigquery"])
    _project(root, other, "Dashboard tag filter design", tags=["dashboard-design"])
    found = suggest_tags(root, "Dashboard tag filter work", repository)
    names = [item["tag"] for item in found]
    assert names.index("bigquery") < names.index("reporting") < names.index("dashboard-design")
    sources = {item["tag"]: item["source"] for item in found}
    assert sources["bigquery"] == "repository"
    assert sources["dashboard-design"] == "title-similar"
    assert sources["work"] == "title-word"
    assert len(names) == len(set(names))
    assert next(item for item in found if item["tag"] == "bigquery")["projects"]


def test_suggestions_fall_back_to_title_words(root: Path, repository: Path) -> None:
    found = suggest_tags(root, "Skyscanner reward audit", repository)
    assert [item["tag"] for item in found] == ["skyscanner", "reward", "audit"]
    assert {item["source"] for item in found} == {"title-word"}


def test_suggestions_skip_stop_words_short_words_and_words_too_long_for_a_tag(root: Path, repository: Path) -> None:
    found = suggest_tags(root, "The new ab " + "x" * 30 + " project", repository)
    assert [item["tag"] for item in found] == ["project"]


def test_suggestions_respect_the_limit_and_skip_invalid_rows(root: Path, repository: Path) -> None:
    (root / "2026-01-01-009").mkdir()
    (root / "2026-01-01-009" / "project.json").write_text("{not json", encoding="utf-8")
    assert len(suggest_tags(root, "alpha beta gamma delta", repository, limit=2)) == 2
    assert suggest_tags(root, "", repository) == []
    with pytest.raises(WorkspaceError, match="limit"):
        suggest_tags(root, "alpha", repository, limit=0)


def test_suggestions_read_past_one_hundred_projects(root: Path, repository: Path) -> None:
    for number in range(101):
        directory = root / f"2026-02-01-{number:03d}"
        directory.mkdir()
        atomic_write_json(
            directory / "project.json",
            {"project": directory.name, "title": f"Job {number}", "status": "DONE",
             "working_directory": str(repository), "tags": [f"tag-{number:03d}"] if number == 100 else []},
        )
    assert "tag-100" in [item["tag"] for item in suggest_tags(root, "Something else", repository, limit=50)]


# --- the tag command -----------------------------------------------------------------------------


def _tag(project: Path, action: str, *names: str, revision: int | None = None) -> dict[str, Any]:
    expected = _state(project)["revision"] if revision is None else revision
    return tag_project(project, action, list(names), expected_revision=expected)


def test_tag_add_and_remove_bump_the_revision(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha"])
    added = _tag(project, "add", "Beta Tag")
    assert added["tags"] == ["alpha", "beta-tag"]
    assert added["revision"] == 1
    removed = _tag(project, "remove", "alpha")
    assert removed["tags"] == ["beta-tag"] and removed["revision"] == 2
    assert _state(project)["tags"] == ["beta-tag"]


def test_tag_add_works_on_a_project_without_a_tags_field(root: Path, repository: Path) -> None:
    project = _project(root, repository)
    assert _tag(project, "add", "first")["tags"] == ["first"]


def test_tag_refusals_start_with_tag_refused_and_write_nothing(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha", "beta"])
    before = (project / "project.json").read_text(encoding="utf-8")
    cases = [
        ("add", ["alpha"], "already"),
        ("add", ["Bad!"], "tag"),
        ("remove", ["missing"], "not"),
        ("remove", ["alpha", "beta"], "at least 1"),
        ("add", [f"t{i}" for i in range(4)], "at most 5"),
    ]
    for action, names, fragment in cases:
        with pytest.raises(WorkspaceError, match=r"^tag refused: .*" + fragment):
            tag_project(project, action, names, expected_revision=0)
    assert (project / "project.json").read_text(encoding="utf-8") == before


def test_the_execution_refusal_points_to_the_legacy_executor_notes(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha"])
    state = _state(project)
    state["execution"]["attempts"] = {"T01": {"n": 1}}
    atomic_write_json(project / "project.json", state)
    with pytest.raises(WorkspaceError, match=r"^tag refused: .*references/legacy-executor\.md"):
        tag_project(project, "add", ["x"], expected_revision=0)


def test_an_os_error_is_still_a_tag_refusal(root: Path, repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import workspace_lib

    project = _project(root, repository, tags=["alpha"])

    def broken(*args: Any, **options: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(workspace_lib, "commit_state", broken)
    with pytest.raises(WorkspaceError, match=r"^tag refused: .*disk full"):
        tag_project(project, "add", ["x"], expected_revision=0)


def test_tag_refuses_an_unknown_action_and_an_empty_list(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha"])
    with pytest.raises(WorkspaceError, match=r"^tag refused: unknown tag action"):
        tag_project(project, "rename", ["x"], expected_revision=0)
    with pytest.raises(WorkspaceError, match=r"^tag refused: name at least one tag"):
        tag_project(project, "add", [], expected_revision=0)


def test_tag_remove_of_the_last_tag_is_refused(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["only"])
    with pytest.raises(WorkspaceError, match="at least 1"):
        _tag(project, "remove", "only")


def test_tag_refuses_a_stale_revision_as_a_conflict(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha"])
    with pytest.raises(WorkspaceConflict, match=r"^tag refused:"):
        tag_project(project, "add", ["beta"], expected_revision=5)


def test_tag_refuses_a_v3_project_and_names_migrate(root: Path, repository: Path) -> None:
    project = _project(root, repository)
    state = _state(project)
    state["schema_version"] = 3
    del state["execution"]
    atomic_write_json(project / "project.json", state)
    with pytest.raises(WorkspaceError, match=r"^tag refused: .*migrate"):
        tag_project(project, "add", ["x"], expected_revision=0)


def test_tag_refuses_a_malformed_stored_field_instead_of_overwriting_it(root: Path, repository: Path) -> None:
    project = _project(root, repository)
    _rewrite(project, tags="oops")
    with pytest.raises(WorkspaceError, match=r"^tag refused: .*project\.json"):
        tag_project(project, "add", ["x"], expected_revision=0)
    assert _state(project)["tags"] == "oops"


def test_tag_follows_the_commit_guard_for_execution(root: Path, repository: Path) -> None:
    for change in ({"coordinator_run": "run-1"}, {"attempts": {"T01": {"n": 1}}}):
        project = _project(root, repository, f"Guarded {len(change)} {next(iter(change))}")
        state = _state(project)
        state["execution"].update(change)
        atomic_write_json(project / "project.json", state)
        with pytest.raises(WorkspaceError, match=r"^tag refused: .*(attempts|coordinator_run)"):
            tag_project(project, "add", ["x"], expected_revision=0)


def _finished(project: Path) -> None:
    """A real finished project: one justified skipped task and a reflection with its retro section."""
    state = _state(project)
    state["status"] = "DONE"
    state["tasks"] = [{
        "id": "T01", "name": "Nothing to do", "status": "SKIPPED", "depends_on": [], "outputs": [],
        "success_criteria": "None.", "verification": "None.", "evidence": [],
        "effect": {"kind": "none", "description": None},
        "authorization": {
            "required": False, "status": "not_required", "scope": None, "source": None, "authorized_at": None,
        },
        "receipts": [], "skip_reason": "Not needed for this test project.", "block_reason": None,
    }]
    atomic_write_json(project / "project.json", state)
    (project / "reflection.md").write_text(
        "# Reflection\n\nThe project finished with nothing to do, which is all this fixture needs to say.\n\n"
        "## Retro\n\nWhat got in the way: nothing. References loaded and not needed: none. "
        "One plugin change: none.\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("status", ["DONE", "EXECUTING", "ALIGNING"])
def test_tag_add_works_in_each_state(root: Path, repository: Path, status: str) -> None:
    project = _project(root, repository, f"State {status}")
    if status == "DONE":
        _finished(project)
    elif status == "EXECUTING":
        _rewrite(project, status="EXECUTING")
    assert _state(project)["status"] == status
    assert _tag(project, "add", "marked")["tags"] == ["marked"]
    assert _state(project)["status"] == status


def test_update_and_reconcile_refuse_tags(root: Path, repository: Path) -> None:
    from workspace_session import prepare_update

    state = _state(_project(root, repository))
    with pytest.raises(WorkspaceError, match="unsupported update fields"):
        prepare_update(state, {"tags": ["x"]})
    with pytest.raises(WorkspaceError, match="unsupported update fields"):
        prepare_update(state, {"tags": ["x"]}, internal=True)


def test_the_stored_json_is_plain(root: Path, repository: Path) -> None:
    project = _project(root, repository, tags=["alpha"])
    assert json.loads((project / "project.json").read_text(encoding="utf-8"))["tags"] == ["alpha"]
