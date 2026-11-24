"""The per-task record `observability/<task>.json`: its shape, its numbers and what the validator refuses."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import workspace_observe as observe

FILE = {"path": "src/app.py", "added": 3, "removed": 1, "language": "python", "kind": "source"}
FINDING = {"code": "bypass-added", "file": "src/app.py", "line": 4, "detail": "# noqa"}
OVER = {"file": "src/app.py", "function": "big", "line": 10, "complexity": 14}


def build(**changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "task": "T03",
        "base_commit": "a" * 40,
        "head_tree": "b" * 64,
        "run": "gates-t03-1",
        "commands_sha256": "c" * 64,
        "files": [FILE],
        "tests": {"files": 2, "cases": 9},
        "covered": 9,
        "total": 10,
        "complexity": {"touched": 2, "max": 14, "over_limit": [OVER]},
        "scan": {"findings": [FINDING], "info": []},
        "failures": ["bypass-added: src/app.py:4 noqa"],
    }
    arguments.update(changes)
    return observe.build_record(**arguments)


def test_build_record_holds_the_design_shape_and_the_percent() -> None:
    record = build()
    assert set(record) == {
        "task",
        "base_commit",
        "head_tree",
        "run",
        "commands_sha256",
        "files",
        "tests",
        "diff_coverage",
        "complexity",
        "scan",
        "failures",
    }
    assert record["diff_coverage"] == {"covered": 9, "total": 10, "percent": 90.0}
    assert observe.validate_record(record) == []


def test_no_changed_executable_lines_is_one_hundred_percent_and_says_none() -> None:
    record = build(covered=0, total=0)
    assert record["diff_coverage"] == {"covered": 0, "total": 0, "percent": 100.0, "note": "none"}
    assert observe.validate_record(record) == []


def test_a_note_other_than_none_is_kept_for_unmeasured_coverage() -> None:
    record = build(covered=0, total=0, note="waived")
    assert record["diff_coverage"]["note"] == "waived"
    assert observe.validate_record(record) == []


def test_files_are_sorted_by_path() -> None:
    other = {**FILE, "path": "a.py"}
    assert [item["path"] for item in build(files=[FILE, other])["files"]] == ["a.py", "src/app.py"]


def test_the_record_round_trips_through_json_unchanged() -> None:
    import json

    record = build()
    assert json.loads(json.dumps(record)) == record


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("task",), "", "task must be non-empty text"),
        (("base_commit",), "xyz", "base_commit must be 40 lowercase hex digits"),
        (("head_tree",), "xyz", "head_tree must be 64 lowercase hex digits"),
        (("commands_sha256",), 3, "commands_sha256 must be 64 lowercase hex digits"),
        (("run",), "", "run must be non-empty text"),
        (("files",), "x", "files must be a list"),
        (("files", 0), "x", "files[0] must be an object"),
        (("files", 0, "added"), -1, "files[0].added must be a non-negative integer"),
        (("files", 0, "removed"), True, "files[0].removed must be a non-negative integer"),
        (("files", 0, "kind"), "weird", "files[0].kind must be one of"),
        (("files", 0, "language"), "", "files[0].language must be non-empty text"),
        (("files", 0, "path"), 4, "files[0].path must be non-empty text"),
        (("tests",), [], "tests must be an object"),
        (("tests", "cases"), -2, "tests.cases must be a non-negative integer"),
        (("diff_coverage",), None, "diff_coverage must be an object"),
        (("diff_coverage", "percent"), 120, "diff_coverage.percent must be a number from 0 to 100"),
        (("diff_coverage", "covered"), 11, "diff_coverage.covered cannot exceed total"),
        (("diff_coverage", "note"), 3, "diff_coverage.note must be text"),
        (("complexity",), "x", "complexity must be an object"),
        (("complexity", "touched"), "2", "complexity.touched must be a non-negative integer"),
        (("complexity", "max"), "14", "complexity.max must be null or a non-negative integer"),
        (("complexity", "over_limit"), {}, "complexity.over_limit must be a list"),
        (("complexity", "over_limit", 0), 1, "complexity.over_limit[0] must be an object"),
        (("complexity", "over_limit", 0, "complexity"), 0, "complexity.over_limit[0].complexity must be a positive"),
        (("failures",), "x", "failures must be a list of text"),
        (("failures", 0), 3, "failures must be a list of text"),
        (("scan",), 1, "scan must be an object"),
        (("scan", "findings"), "x", "scan.findings must be a list"),
        (("scan", "findings", 0), "x", "scan.findings[0] must be an object"),
        (("scan", "info"), None, "scan.info must be a list"),
    ],
)
def test_the_validator_names_each_malformed_part(path: tuple[Any, ...], value: Any, message: str) -> None:
    record = copy.deepcopy(build())
    target: Any = record
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    assert any(message in error for error in observe.validate_record(record)), observe.validate_record(record)


def test_the_validator_reports_missing_and_unexpected_keys_and_a_non_object() -> None:
    record = build()
    del record["tests"]
    record["extra"] = 1
    errors = observe.validate_record(record)
    assert "record: missing fields: tests" in errors
    assert "record: unexpected fields: extra" in errors
    assert observe.validate_record([]) == ["record must be an object"]
    assert observe.validate_record(None) == ["record must be an object"]


def test_a_missing_nested_key_is_named_with_its_object() -> None:
    record = build()
    del record["complexity"]["touched"]
    del record["scan"]["info"]
    errors = observe.validate_record(record)
    assert "complexity: missing fields: touched" in errors
    assert "scan: missing fields: info" in errors


@pytest.mark.parametrize("key", ["files", "tests", "diff_coverage", "complexity", "scan", "failures", "run"])
def test_each_missing_top_level_key_is_reported_once_and_does_not_raise(key: str) -> None:
    record = build()
    del record[key]
    assert observe.validate_record(record) == [f"record: missing fields: {key}"]


def test_a_missing_over_limit_and_a_negative_covered_count_are_named() -> None:
    record = build()
    del record["complexity"]["over_limit"]
    record["diff_coverage"]["covered"] = -1
    errors = observe.validate_record(record)
    assert "complexity: missing fields: over_limit" in errors
    assert "diff_coverage.covered must be a non-negative integer" in errors
