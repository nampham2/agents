"""Read CI job names from workflow files by indentation, without a YAML parser.

Imports nothing from the workspace, so any module can use it. It reads only the shapes a
required-check comparison needs; anything else is noted, never guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Top-level keys of `.gitlab-ci.yml` that configure the pipeline and are not jobs.
GITLAB_RESERVED_KEYS = frozenset({
    "after_script", "before_script", "cache", "default", "image", "include", "services", "stages",
    "variables", "workflow",
})

KEY_LINE = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#:][^#:]*?)\s*:(?:\s+(?P<value>.*))?$""")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _significant(lines: list[str]) -> list[tuple[int, str]]:
    """Non-blank, non-comment lines with their indentation, in order."""
    return [(_indent(line), line.strip()) for line in lines if line.strip() and not line.strip().startswith("#")]


def _unquote(value: str) -> str:
    value = value.strip()
    if value[:1] in "\"'":
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.sub(r"\s+#.*$", "", value).strip()


def _key(text: str) -> tuple[str, str] | None:
    match = KEY_LINE.match(text)
    if not match:
        return None
    return _unquote(match["key"]), match["value"] or ""


def _github_jobs(text: str) -> tuple[list[tuple[str, bool]], list[str]]:
    """(name, reportable) per job of one workflow, and notes. A tab in indentation skips the file."""
    lines = text.splitlines()
    if any(re.match(r" *\t", line) for line in lines):
        return [], ["a tab in indentation"]
    start = next((index for index, line in enumerate(lines) if re.match(r"jobs\s*:\s*(#.*)?$", line)), None)
    if start is None:
        return [], []
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.strip().startswith("#") and _indent(line) == 0:
            break
        block.append(line)
    rows = _significant(block)
    if not rows:
        return [], []
    job_indent = rows[0][0]
    bodies: list[tuple[str, list[tuple[int, str]]]] = []
    for depth, content in rows:
        parsed = _key(content) if depth == job_indent else None
        if parsed:
            bodies.append((parsed[0], []))
        elif bodies and depth > job_indent:
            bodies[-1][1].append((depth, content))
    found: list[tuple[str, bool]] = []
    for key, body in bodies:
        child = body[0][0] if body else None
        name, reportable = key, True
        for depth, content in body:
            if depth != child:
                continue
            parsed = _key(content)
            if parsed is None:
                continue
            if parsed[0] == "name":
                name = _unquote(parsed[1]) or key
            elif parsed[0] == "uses":
                reportable = False
            elif parsed[0] == "strategy" and any(
                deeper > child and content_deeper.startswith("matrix") for deeper, content_deeper in body
            ):
                reportable = False
        if "${{" in name:
            reportable = False
        found.append((name, reportable))
    return found, []


def _gitlab_jobs(text: str) -> tuple[list[str], bool]:
    """(job keys, whether `include:` was seen) of one `.gitlab-ci.yml`."""
    names: list[str] = []
    included = False
    for depth, content in _significant(text.splitlines()):
        parsed = _key(content) if depth == 0 else None
        if parsed is None:
            continue
        if parsed[0] == "include":
            included = True
        if not parsed[0].startswith(".") and parsed[0] not in GITLAB_RESERVED_KEYS:
            names.append(parsed[0])
    return names, included


def ci_checks(root: Path | None, required: list[str] | None, *, reason: str | None = None) -> dict[str, Any]:
    """Compare required check names with the job names the CI files under `root` report.

    `missing` lists the required names no reportable job carries. A job whose name comes from a matrix,
    an expression or a reusable workflow cannot be matched by name; `unreportable` lists those, so a
    name in `missing` may still be produced at run time. The result always has every key.
    """
    result: dict[str, Any] = {
        "required": required, "root": str(root) if root else None, "files": [], "skipped": [],
        "jobs": [], "unreportable": [], "matched": [], "missing": [], "notes": [],
    }
    notes: list[str] = result["notes"]
    if required is None:
        notes.append("required_checks was not supplied; nothing to compare")
    if root is None:
        notes.append(reason or "no active target worktree; no CI file was read")
        return result
    jobs: list[str] = result["jobs"]
    github = root / ".github" / "workflows"
    paths = sorted([*github.glob("*.yml"), *github.glob("*.yaml")]) if github.is_dir() else []
    gitlab = root / ".gitlab-ci.yml"
    if gitlab.is_file():
        paths.append(gitlab)
    for path in paths:
        relative = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            result["skipped"].append({"file": relative, "reason": f"unreadable: {error.__class__.__name__}"})
            continue
        if path == gitlab:
            names, included = _gitlab_jobs(text)
            jobs.extend(names)
            if included:
                notes.append(f"{relative} has include:; included jobs are not listed")
        else:
            found, skipped = _github_jobs(text)
            if skipped:
                result["skipped"].append({"file": relative, "reason": skipped[0]})
                continue
            for name, reportable in found:
                (jobs if reportable else result["unreportable"]).append(name)
            if any(not reportable for _, reportable in found):
                notes.append(f"{relative} has a job named by a matrix, expression or reusable workflow")
        result["files"].append(relative)
    if not paths:
        notes.append("no CI file found")
    for name in required or []:
        (result["matched"] if name in jobs else result["missing"]).append(name)
    return result
