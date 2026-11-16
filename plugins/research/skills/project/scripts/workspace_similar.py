"""Likely-duplicate detection for a new project.

`init` asks this module before it creates a directory. A live project whose title overlaps the new
title blocks `init` until the user acknowledges it; a finished one is only listed. The module reads
the discovery rows of `list_projects` and nothing else, so it adds no new file parsing and treats an
unreadable or legacy row as no evidence.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from workspace_lib import WorkspaceError, normalize_tag
from workspace_session import list_projects

# Statuses that mean the project is still being worked on: a match is blocking.
LIVE_STATUSES = frozenset({"ALIGNING", "PLANNING", "EXECUTING", "REVIEW", "BLOCKED"})
# Finished projects: a match is listed as related and never blocks.
TERMINAL_STATUSES = frozenset({"DONE", "CANCELLED"})
# Similarity at which two titles are a likely duplicate. Calibrated on 119 workspace titles and 15
# paraphrases of them: 0.35 catches all 15 with 2 false positives in 8,791 comparisons, both finished
# projects that only warn. A raw overlap coefficient flagged 34 pairs at the recall these reach.
SAME_REPOSITORY_SCORE = 0.35
ANYWHERE_SCORE = 0.5
MINIMUM_TOKEN_LENGTH = 3
PAGE_SIZE = 100
STOP_WORDS = frozenset(
    "the and for with from into onto over under this that these those your our its how what when why "
    "are was were has have had not but any all new use using via per vs".split()
)
SUFFIXES = ("ing", "ed", "es", "s")
WORKTREE_SUFFIX = ".worktrees"


def tokens(text: str) -> frozenset[str]:
    """Significant lowercase words of a title: no stop words, no short words, one suffix stripped."""
    result = set()
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        if len(word) < MINIMUM_TOKEN_LENGTH or word in STOP_WORDS:
            continue
        for suffix in SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= MINIMUM_TOKEN_LENGTH:
                word = word[: -len(suffix)]
                break
        result.add(word)
    return frozenset(result)


def document_frequencies(titles: list[frozenset[str]]) -> dict[str, int]:
    """How many titles contain each word; common words ("research", "skill") then carry little weight."""
    frequencies: dict[str, int] = {}
    for title in titles:
        for word in title:
            frequencies[word] = frequencies.get(word, 0) + 1
    return frequencies


def similarity(first: frozenset[str], second: frozenset[str], frequencies: dict[str, int], total: int) -> float:
    """Weighted Jaccard: the weight of shared words over the weight of all words; 0 when either is empty.

    A word weighs `ln((total + 1) / (frequency + 1)) + 1`, so a word that most titles share counts for
    little and a rare word counts for more. Two titles with the same words score 1.
    """
    if not first or not second:
        return 0.0

    def weight(words: frozenset[str]) -> float:
        return sum(math.log((total + 1) / (frequencies.get(word, 0) + 1)) + 1 for word in words)

    return weight(first & second) / weight(first | second)


def repository_of(path: str) -> str:
    """The repository a working directory belongs to: a `<repo>.worktrees/<id>` path maps to `<repo>`.

    A project that records its worktree repoints its working directory there, so comparing raw paths
    would miss two projects on the same repository.
    """
    location = Path(path)
    if location.parent.name.endswith(WORKTREE_SUFFIX):
        return str(location.parent.parent / location.parent.name[: -len(WORKTREE_SUFFIX)])
    return str(location)


def _rows(workspace_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = list_projects(workspace_root, limit=PAGE_SIZE, offset=offset)
        rows.extend(page["projects"])
        offset = page["next_offset"]
    return rows


def find_similar(workspace_root: Path, title: str, working_directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Projects whose title is a likely duplicate of `title`, split into `blocking` and `related`.

    Best match first. A project counts when its similarity reaches `SAME_REPOSITORY_SCORE` on the same
    repository or `ANYWHERE_SCORE` anywhere. Word weights come from the titles of the workspace.
    """
    new_tokens = tokens(title)
    repository = repository_of(str(working_directory.resolve()))
    known = [row for row in _rows(workspace_root) if row.get("status") in LIVE_STATUSES | TERMINAL_STATUSES]
    scored = [(row, tokens(row["title"])) for row in known]
    frequencies = document_frequencies([words for _, words in scored])
    blocking: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    for row, words in scored:
        status = row["status"]
        score = similarity(new_tokens, words, frequencies, len(known))
        same = repository_of(row["working_directory"]) == repository
        if score < (SAME_REPOSITORY_SCORE if same else ANYWHERE_SCORE):
            continue
        match = {
            "project": row["project"], "title": row["title"], "status": status, "score": round(score, 2),
            "same_repository": same, "path": row["path"], "tags": row.get("tags", []),
        }
        (blocking if status in LIVE_STATUSES else related).append(match)
    for matches in (blocking, related):
        matches.sort(key=lambda match: (-match["score"], match["project"]))
    return {"blocking": blocking, "related": related}


def suggest_tags(
    workspace_root: Path, title: str, working_directory: Path, limit: int = 10
) -> list[dict[str, Any]]:
    """Tags to offer for a new project, best first, each once and every one valid.

    Three ordered groups: tags of projects on the same repository (most used first), tags of projects with a
    similar title (best score first; `find_similar` scored them), then the significant words of the title in
    the order they appear. A tag appears in the first group that has it. Unreadable and legacy rows have no
    working directory and no tags, so they are skipped like `find_similar` skips them.
    """
    if not 1 <= limit <= 50:
        raise WorkspaceError("limit must be between 1 and 50")
    repository = repository_of(str(Path(working_directory).resolve()))
    rows = [row for row in _rows(workspace_root) if row.get("status") in LIVE_STATUSES | TERMINAL_STATUSES]
    suggestions: dict[str, dict[str, Any]] = {}

    def offer(tag: str, source: str, projects: list[str]) -> None:
        if tag not in suggestions:
            suggestions[tag] = {"tag": tag, "source": source, "projects": projects[:5]}

    on_repository: dict[str, list[str]] = {}
    for row in rows:
        if repository_of(row.get("working_directory", "")) == repository:
            for tag in row.get("tags", []):
                on_repository.setdefault(tag, []).append(row["project"])
    for tag in sorted(on_repository, key=lambda name: (-len(on_repository[name]), name)):
        offer(tag, "repository", on_repository[tag])

    found = find_similar(workspace_root, title, working_directory)
    similar: dict[str, list[str]] = {}
    for match in sorted(found["blocking"] + found["related"], key=lambda item: (-item["score"], item["project"])):
        for tag in match["tags"]:
            similar.setdefault(tag, []).append(match["project"])
    for tag, projects in similar.items():
        offer(tag, "title-similar", projects)

    for word in re.findall(r"[a-z0-9]+", title.casefold()):
        if len(word) < MINIMUM_TOKEN_LENGTH or word in STOP_WORDS:
            continue
        try:
            offer(normalize_tag(word), "title-word", [])
        except WorkspaceError:
            continue
    return list(suggestions.values())[:limit]


def check_new_project(
    workspace_root: Path, title: str, working_directory: Path, acknowledged: list[str]
) -> list[str]:
    """Refuse `init` while a live project looks like a duplicate the user has not acknowledged.

    Returns warnings that list the finished projects that look alike. An acknowledged id must name a
    blocking match, so a stale or mistyped acknowledgment cannot pass for the user's answer.
    """
    if not workspace_root.is_dir():
        return []
    found = find_similar(workspace_root, title, working_directory)
    blocking = {match["project"]: match for match in found["blocking"]}
    unknown = [project for project in acknowledged if project not in blocking]
    if unknown:
        raise WorkspaceError(
            f"--acknowledge-duplicates names {', '.join(unknown)}, which is not a live project that looks like "
            "this title; acknowledge only the projects init listed"
        )
    unacknowledged = [match for project, match in blocking.items() if project not in acknowledged]
    if unacknowledged:
        lines = [
            f"  {match['project']} {match['status']} score {match['score']}"
            f"{' same repository' if match['same_repository'] else ''}: {match['title']}"
            for match in unacknowledged
        ]
        raise WorkspaceError(
            "init created nothing: a live project looks like a duplicate of this title:\n"
            + "\n".join(lines)
            + "\nShow these to the user and ask whether to resume one, continue with a new project, or cancel. "
            "Only after the user chooses a new project, rerun init with --acknowledge-duplicates "
            + " ".join(match["project"] for match in unacknowledged)
        )
    return [
        f"finished project {match['project']} ({match['status']}, score {match['score']}) looks like this "
        f"title: {match['title']}"
        for match in found["related"]
    ]
