"""Ranked retrieval over the workspace memory layer, stdlib only.

The shipped substring search this replaces matched six frontmatter lines per topic and every line of
every post-mortem, unranked. Measured on the live workspace (`docs/memory-management-review.md`) it
found an expected topic in 6% of queries and returned a median 18.7 KB per query, of which a median
140 lines were post-mortem matches and one was a topic. Indexing topic bodies is what carries the
signal: frontmatter-only ranking scored 0.34 recall at 5 against 0.65 for BM25 over whole topics.

So this module ranks whole topics with BM25 over name, description, scope, keywords and body, and
excerpts the best paragraph, which is the granularity that knew *where* the match was. It is rebuilt
per call: at tens of topics that costs milliseconds, and a cached index would add a staleness story
for no measurable gain. Nothing here needs a model, a daemon, a compiled extension or a host hook,
which is the constraint every alternative failed (`references/memory-architecture.md`).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from workspace_lib import (
    MEMORY_FRONTMATTER_DELIMITER,
    PROJECT_ID_PATTERN,
    MemoryTopic,
    WorkspaceError,
    load_memory_topics,
    memory_topic_body,
    read_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Function words plus the vocabulary every project record shares. A term that appears in nearly
# every topic and every query cannot separate them, and BM25's idf already discounts it; the list
# exists so a two-word query is not spent on "the project".
STOP_WORDS = frozenset(
    """
    a an the and or of to in on for with by from at as is are was were be been being it its this that
    these those which who whom whose what when where why how not no nor so than then there their them
    they we you your our i me my he she his her him do does did done doing have has had having will
    would shall should can could may might must into onto over under about after before between during
    without within through per via each every any all some such only also just more most less least
    very much many few one two three first second new old same other another own both either neither
    because while until since if unless whether but yet however therefore thus hence up down out off
    again further once here now already still ever never always often project projects task tasks
    work working file files use used using run runs running make makes made get gets set sets need
    needs needed
    """.split()
)
TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.\-]*[a-z0-9]|[a-z0-9]")
EXCERPT_MAX_CHARS = 300
CANDIDATE_LIMIT = 3
CANDIDATE_MAX_BYTES = 600
CANDIDATE_DESCRIPTION_CHARS = 120
BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, minus stop words, single characters, digits, project ids and a trailing plural."""
    tokens: list[str] = []
    for raw in TOKEN_PATTERN.findall(text.lower()):
        token = raw.strip("._-")
        # Two-letter terms stay: `uv`, `bq`, `ci` and `gh` are real vocabulary in this workspace.
        if len(token) < 2 or token in STOP_WORDS or token.isdigit() or PROJECT_ID_PATTERN.match(token):
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            token = token[:-1]
        tokens.append(token)
    return tokens


class Bm25Index:
    """Okapi BM25 over pre-tokenized documents. Scores are query-dependent and not comparable across corpora."""

    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = BM25_K1, b: float = BM25_B) -> None:
        self.k1 = k1
        self.b = b
        self.lengths = [len(document) for document in documents]
        self.size = len(documents)
        self.average_length = (sum(self.lengths) / self.size) if self.size else 1.0
        self.frequencies = [Counter(document) for document in documents]
        self.document_frequency: Counter[str] = Counter()
        for document in documents:
            self.document_frequency.update(set(document))

    def idf(self, token: str) -> float:
        seen = self.document_frequency.get(token, 0)
        return math.log(1 + (self.size - seen + 0.5) / (seen + 0.5))

    def score(self, query: Sequence[str], index: int) -> float:
        frequencies = self.frequencies[index]
        length = self.lengths[index]
        total = 0.0
        for token in set(query):
            occurrences = frequencies.get(token)
            if not occurrences:
                continue
            normalized = occurrences * (self.k1 + 1) / (
                occurrences + self.k1 * (1 - self.b + self.b * length / self.average_length)
            )
            total += self.idf(token) * normalized
        return total


@dataclass
class IndexedTopic:
    """One parseable topic with its body split into paragraphs that remember their file line."""

    topic: MemoryTopic
    body: str
    paragraphs: list[tuple[int, str]] = field(default_factory=list)

    @property
    def document(self) -> str:
        pieces = [self.topic.name.replace("-", " "), self.topic.description, self.topic.scope]
        if self.topic.keywords:
            pieces.append(self.topic.keywords)
        pieces.append(self.body)
        return "\n".join(pieces)


@dataclass
class MemoryHit:
    topic: str
    score: float
    description: str
    scope: str
    kind: str
    status: str
    excerpt: str
    path: Path
    line: int

    def as_json(self, workspace_root: Path) -> dict[str, object]:
        return {
            "topic": self.topic,
            "score": round(self.score, 3),
            "description": self.description,
            "scope": self.scope,
            "kind": self.kind,
            "status": self.status,
            "excerpt": self.excerpt,
            "path": str(self.path.relative_to(workspace_root)),
            "line": self.line,
        }


def _paragraphs_with_lines(content: str) -> list[tuple[int, str]]:
    """Body paragraphs with the 1-based file line each starts on, skipping the frontmatter block."""
    lines = content.splitlines()
    start = 0
    if lines and lines[0].strip() == MEMORY_FRONTMATTER_DELIMITER:
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == MEMORY_FRONTMATTER_DELIMITER:
                start = index + 1
                break
    paragraphs: list[tuple[int, str]] = []
    current: list[str] = []
    first_line = 0
    for number, line in enumerate(lines[start:], start=start + 1):
        if line.strip():
            if not current:
                first_line = number
            current.append(line.strip())
        elif current:
            paragraphs.append((first_line, " ".join(current)))
            current = []
    if current:
        paragraphs.append((first_line, " ".join(current)))
    return paragraphs


def load_indexed_topics(workspace_root: Path, *, include_retired: bool = False) -> list[IndexedTopic]:
    """Every parseable, active topic with its body. Malformed files are skipped, as generation skips them."""
    topics, _ = load_memory_topics(workspace_root)
    indexed: list[IndexedTopic] = []
    for topic in topics:
        if topic.status == "retired" and not include_retired:
            continue
        try:
            content = read_text(topic.path)
        except WorkspaceError:
            continue
        indexed.append(
            IndexedTopic(topic=topic, body=memory_topic_body(content), paragraphs=_paragraphs_with_lines(content))
        )
    return indexed


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_MAX_CHARS else text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"


def rank_indexed(indexed: Sequence[IndexedTopic], query: str) -> list[MemoryHit]:
    """Rank topics by whole-document BM25; excerpt each hit's best paragraph. Zero-score topics are dropped."""
    query_tokens = tokenize(query)
    if not query_tokens or not indexed:
        return []
    topic_index = Bm25Index([tokenize(item.document) for item in indexed])
    paragraph_owner: list[int] = []
    paragraph_tokens: list[list[str]] = []
    for position, item in enumerate(indexed):
        for _line, text in item.paragraphs:
            paragraph_owner.append(position)
            paragraph_tokens.append(tokenize(text))
    paragraph_index = Bm25Index(paragraph_tokens)
    best_paragraph: dict[int, tuple[float, int]] = {}
    for number, owner in enumerate(paragraph_owner):
        score = paragraph_index.score(query_tokens, number)
        if score > best_paragraph.get(owner, (0.0, -1))[0]:
            best_paragraph[owner] = (score, number)
    hits: list[MemoryHit] = []
    for position, item in enumerate(indexed):
        score = topic_index.score(query_tokens, position)
        if score <= 0:
            continue
        chosen = best_paragraph.get(position)
        if chosen is None:
            line, text = (item.paragraphs[0] if item.paragraphs else (1, item.topic.description))
        else:
            offset = chosen[1] - paragraph_owner.index(position)
            line, text = item.paragraphs[offset]
        hits.append(
            MemoryHit(
                topic=item.topic.name,
                score=score,
                description=item.topic.description,
                scope=item.topic.scope,
                kind=item.topic.kind,
                status=item.topic.status,
                excerpt=_excerpt(text),
                path=item.topic.path,
                line=line,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.topic))
    return hits


def rank_topics(workspace_root: Path, query: str, *, include_retired: bool = False) -> list[MemoryHit]:
    if not query.strip():
        raise WorkspaceError("search-memory needs a non-empty query")
    return rank_indexed(load_indexed_topics(workspace_root, include_retired=include_retired), query)


def search_postmortems(workspace_root: Path, query: str) -> list[tuple[Path, int, str]]:
    """Case-insensitive substring hits in per-project post-mortems: (path, line number, line).

    Opt-in from the CLI. Measured against the live root, post-mortem lines outnumbered topic hits a
    hundred to one, so they are never mixed into the ranked default.
    """
    needle = query.strip().lower()
    if not needle:
        raise WorkspaceError("search-memory needs a non-empty query")
    hits: list[tuple[Path, int, str]] = []
    if not workspace_root.is_dir():
        return hits
    for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
        postmortem = child / "reflection.md"
        if not child.is_dir() or child.name.startswith(".") or not postmortem.is_file():
            continue
        try:
            content = read_text(postmortem)
        except WorkspaceError:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if needle in line.lower():
                hits.append((postmortem, number, line.strip()))
    return hits


def similar_topics(workspace_root: Path, text: str, *, limit: int = CANDIDATE_LIMIT) -> list[MemoryHit]:
    return rank_indexed(load_indexed_topics(workspace_root), text)[:limit]


def creation_gate(workspace_root: Path, slug: str, text: str, *, create: bool) -> None:
    """Refuse to create a new topic file unless the caller asked for one after seeing what already exists.

    The measured failure was not bad topics but too many of them: one drain created three, breached
    the index budget, and spent its closing time merging by hand. So a new slug pauses once, with
    the nearest existing topics in the refusal, and `--create` is the caller's answer.
    """
    if create or (workspace_root / "memory" / f"{slug}.md").is_file():
        return
    hits = similar_topics(workspace_root, text)
    if hits:
        listed = "; ".join(f"{hit.topic} — {hit.description} ({hit.score:.2f})" for hit in hits)
        advice = f"similar existing topics: {listed}. Amend one of them with promote-memory <name>"
    else:
        advice = "no similar topic was found"
    raise WorkspaceError(
        f"refusing to create a new topic {slug!r} without --create; {advice}, "
        "or rerun with --create if this lesson is genuinely new"
    )


def objective_text(spec: str) -> str:
    """The `### Objective and audience` section body, or an empty string."""
    match = re.search(r"^### Objective and audience[ \t]*\n(.*?)(?=^#{1,3} |\Z)", spec, re.S | re.M)
    return match.group(1).strip() if match else ""


def memory_candidates(
    workspace_root: Path,
    *,
    title: str,
    objective: str = "",
    limit: int = CANDIDATE_LIMIT,
    max_bytes: int = CANDIDATE_MAX_BYTES,
) -> list[dict[str, object]]:
    """Top topics for a project's title and objective, bounded in count and in serialized bytes.

    Never raises: this rides on `context`, and a memory defect must not make a project unresumable.
    Returns an empty list when the root has no topics or the query has no usable tokens.
    """
    if not (workspace_root / "memory").is_dir():
        return []
    try:
        hits = rank_indexed(load_indexed_topics(workspace_root), f"{title}\n{objective}")
    except (WorkspaceError, OSError):
        return []
    candidates: list[dict[str, object]] = [
        {"topic": hit.topic, "description": hit.description[:CANDIDATE_DESCRIPTION_CHARS], "score": round(hit.score, 3)}
        for hit in hits[:limit]
    ]
    while candidates and len(json.dumps(candidates).encode("utf-8")) > max_bytes:
        candidates.pop()
    return candidates
