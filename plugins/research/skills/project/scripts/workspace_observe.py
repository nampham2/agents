"""The per-task observability record: a pure builder and validator. Nothing here reads the repository.

The record `observability/<task>.json` is the only home of the numbers a gate run measures; the evidence
journal keeps its hash. It imports nothing from the workspace, so its shape and its validator are tested alone.
"""

from __future__ import annotations

import re
from typing import Any

RECORD_FIELDS = (
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
)
FILE_KINDS = ("source", "test", "config", "doc", "other")
_BASE = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def build_record(
    *,
    task: str,
    base_commit: str,
    head_tree: str,
    run: str,
    commands_sha256: str,
    files: list[dict[str, Any]],
    tests: dict[str, int],
    covered: int,
    total: int,
    complexity: dict[str, Any],
    scan: dict[str, Any],
    failures: list[str],
    note: "str | None" = None,
) -> dict[str, Any]:
    """Assemble the record. No changed executable lines is 100 percent and reads `none`; a `note` replaces it."""
    percent = 100.0 if total == 0 else round(100.0 * covered / total, 2)
    coverage: dict[str, Any] = {"covered": covered, "total": total, "percent": percent}
    if note is not None:
        coverage["note"] = note
    elif total == 0:
        coverage["note"] = "none"
    return {
        "task": task,
        "base_commit": base_commit,
        "head_tree": head_tree,
        "run": run,
        "commands_sha256": commands_sha256,
        "files": sorted(files, key=lambda item: item["path"]),
        "tests": tests,
        "diff_coverage": coverage,
        "complexity": complexity,
        "scan": scan,
        "failures": failures,
    }


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _keys(
    value: dict[str, Any], required: set[str], label: str, errors: list[str], optional: "set[str] | None" = None
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - allowed)
    if missing:
        errors.append(f"{label}: missing fields: {', '.join(missing)}")
    if unexpected:
        errors.append(f"{label}: unexpected fields: {', '.join(unexpected)}")


def _object(record: dict[str, Any], key: str, errors: list[str]) -> "dict[str, Any] | None":
    value = record.get(key)
    if key not in record:
        return None
    if not isinstance(value, dict):
        errors.append(f"{key} must be an object")
        return None
    return value


def _validate_files(record: dict[str, Any], errors: list[str]) -> None:
    files = record.get("files")
    if "files" not in record:
        return
    if not isinstance(files, list):
        errors.append("files must be a list")
        return
    for index, item in enumerate(files):
        label = f"files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        _keys(item, {"path", "added", "removed", "language", "kind"}, label, errors)
        for key in ("path", "language"):
            if key in item and not _text(item[key]):
                errors.append(f"{label}.{key} must be non-empty text")
        for key in ("added", "removed"):
            if key in item and not _count(item[key]):
                errors.append(f"{label}.{key} must be a non-negative integer")
        if "kind" in item and item["kind"] not in FILE_KINDS:
            errors.append(f"{label}.kind must be one of {', '.join(FILE_KINDS)}")


def _validate_tests(record: dict[str, Any], errors: list[str]) -> None:
    tests = _object(record, "tests", errors)
    if tests is None:
        return
    _keys(tests, {"files", "cases"}, "tests", errors)
    for key in ("files", "cases"):
        if key in tests and not _count(tests[key]):
            errors.append(f"tests.{key} must be a non-negative integer")


def _validate_coverage(record: dict[str, Any], errors: list[str]) -> None:
    coverage = _object(record, "diff_coverage", errors)
    if coverage is None:
        return
    _keys(coverage, {"covered", "total", "percent"}, "diff_coverage", errors, {"note"})
    for key in ("covered", "total"):
        if key in coverage and not _count(coverage[key]):
            errors.append(f"diff_coverage.{key} must be a non-negative integer")
    percent = coverage.get("percent")
    if "percent" in coverage and not (
        isinstance(percent, (int, float)) and not isinstance(percent, bool) and 0 <= percent <= 100
    ):
        errors.append("diff_coverage.percent must be a number from 0 to 100")
    if _count(coverage.get("covered")) and _count(coverage.get("total")) and coverage["covered"] > coverage["total"]:
        errors.append("diff_coverage.covered cannot exceed total")
    if "note" in coverage and not isinstance(coverage["note"], str):
        errors.append("diff_coverage.note must be text")


def _validate_complexity(record: dict[str, Any], errors: list[str]) -> None:
    complexity = _object(record, "complexity", errors)
    if complexity is None:
        return
    _keys(complexity, {"touched", "max", "over_limit"}, "complexity", errors)
    if "touched" in complexity and not _count(complexity["touched"]):
        errors.append("complexity.touched must be a non-negative integer")
    if "max" in complexity and complexity["max"] is not None and not _count(complexity["max"]):
        errors.append("complexity.max must be null or a non-negative integer")
    over = complexity.get("over_limit")
    if "over_limit" not in complexity:
        return
    if not isinstance(over, list):
        errors.append("complexity.over_limit must be a list")
        return
    for index, item in enumerate(over):
        label = f"complexity.over_limit[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        _keys(item, {"file", "function", "line", "complexity"}, label, errors)
        if "complexity" in item and not (_count(item["complexity"]) and item["complexity"] > 0):
            errors.append(f"{label}.complexity must be a positive integer")


def _validate_scan(record: dict[str, Any], errors: list[str]) -> None:
    scan = _object(record, "scan", errors)
    if scan is None:
        return
    _keys(scan, {"findings", "info"}, "scan", errors)
    for key in ("findings", "info"):
        if key not in scan:
            continue
        if not isinstance(scan[key], list):
            errors.append(f"scan.{key} must be a list")
            continue
        for index, item in enumerate(scan[key]):
            if not isinstance(item, dict):
                errors.append(f"scan.{key}[{index}] must be an object")


def validate_record(record: object) -> list[str]:
    """Every way `record` departs from the shape; empty when it is sound. Never raises."""
    if not isinstance(record, dict):
        return ["record must be an object"]
    errors: list[str] = []
    _keys(record, set(RECORD_FIELDS), "record", errors)
    for key in ("task", "run"):
        if key in record and not _text(record[key]):
            errors.append(f"{key} must be non-empty text")
    if "base_commit" in record and not (
        isinstance(record["base_commit"], str) and _BASE.fullmatch(record["base_commit"])
    ):
        errors.append("base_commit must be 40 lowercase hex digits")
    for key in ("head_tree", "commands_sha256"):
        if key in record and not (isinstance(record[key], str) and _SHA256.fullmatch(record[key])):
            errors.append(f"{key} must be 64 lowercase hex digits")
    _validate_files(record, errors)
    _validate_tests(record, errors)
    _validate_coverage(record, errors)
    _validate_complexity(record, errors)
    _validate_scan(record, errors)
    failures = record.get("failures")
    if "failures" in record and not (isinstance(failures, list) and all(isinstance(item, str) for item in failures)):
        errors.append("failures must be a list of text")
    return errors
