"""Tests for ranked memory retrieval.

The ranker replaced a substring search that measured 6% recall at 5 and a median 18.7 KB of output
per query on the live workspace. What these tests defend is the shape of that fix: bodies are
indexed, output is bounded and says where to read, post-mortems stay opt-in, retired topics stay
out of the default, and nothing here can make `context` fail.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import memory_search
from memory_search import (
    Bm25Index,
    IndexedTopic,
    creation_gate,
    load_indexed_topics,
    memory_candidates,
    objective_text,
    rank_indexed,
    rank_topics,
    search_postmortems,
    similar_topics,
    tokenize,
)
from workspace_lib import MemoryTopic, WorkspaceError, allocate_project


def _topic(name: str, description: str, scope: str = "anywhere", body: str = "Body.", extra: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "kind: method\n"
        f"scope: {scope}\n"
        "sources: 2026-09-09-002\n"
        "updated: 2026-09-09\n"
        f"{extra}"
        "---\n"
        "\n"
        f"{body}\n"
    )


class SearchRootTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.memory = self.workspace / "memory"
        self.memory.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def write_topic(self, name: str, description: str, **kwargs: str) -> Path:
        path = self.memory / f"{name}.md"
        path.write_text(_topic(name, description, **kwargs), encoding="utf-8")
        return path


class TokenizerTests(unittest.TestCase):
    def test_stop_words_digits_single_characters_and_project_ids_are_dropped(self) -> None:
        self.assertEqual(tokenize("The project 2026-09-09-002 ran 42 checks on uv, a b"), ["ran", "check", "uv"])

    def test_plurals_are_folded_without_touching_double_s(self) -> None:
        self.assertEqual(tokenize("policies fixtures pass class"), ["policy", "fixture", "pass", "class"])

    def test_punctuation_is_stripped_but_inner_hyphens_survive(self) -> None:
        self.assertEqual(tokenize("(set-e) -- grep."), ["set-e", "grep"])


class Bm25Tests(unittest.TestCase):
    def test_an_empty_corpus_scores_nothing_and_has_unit_average_length(self) -> None:
        index = Bm25Index([])
        self.assertEqual(index.average_length, 1.0)
        self.assertEqual(index.size, 0)

    def test_a_rarer_term_carries_more_weight(self) -> None:
        index = Bm25Index([["uv", "lock"], ["uv", "cache"], ["uv", "pytest"]])
        self.assertGreater(index.idf("lock"), index.idf("uv"))
        self.assertGreater(index.score(["lock"], 0), index.score(["lock"], 1))
        self.assertEqual(index.score(["absent"], 0), 0.0)


class RankingTests(SearchRootTestCase):
    def test_a_body_match_ranks_and_the_best_paragraph_becomes_the_excerpt_with_its_line(self) -> None:
        self.write_topic(
            "uv-toolchain",
            "Run everything through uv",
            body="First paragraph about locks.\n\nBare pytest picks the system interpreter, not the venv.\n",
        )
        self.write_topic("git-forge", "Read the sha that ran", body="Pipelines and forges.")
        hits = rank_topics(self.workspace, "pytest interpreter venv")
        self.assertEqual([hit.topic for hit in hits], ["uv-toolchain"])
        self.assertEqual(hits[0].excerpt, "Bare pytest picks the system interpreter, not the venv.")
        self.assertEqual(hits[0].line, 12)
        payload = hits[0].as_json(self.workspace)
        self.assertEqual(payload["path"], "memory/uv-toolchain.md")
        self.assertNotIn("scope", payload)
        self.assertEqual(hits[0].as_json(self.workspace, verbose=True)["status"], "active")

    def test_hits_are_ordered_by_score_then_name(self) -> None:
        self.write_topic("alpha", "uv everywhere", body="uv uv uv.")
        self.write_topic("beta", "uv once", body="Nothing else.")
        self.write_topic("gamma", "unrelated", body="No overlap at all.")
        self.assertEqual([hit.topic for hit in rank_topics(self.workspace, "uv")], ["alpha", "beta"])

    def test_a_frontmatter_only_match_falls_back_to_the_first_paragraph(self) -> None:
        self.write_topic("scoped", "Distinctive descriptor", body="Plain body text here.")
        hits = rank_topics(self.workspace, "distinctive descriptor")
        self.assertEqual(hits[0].excerpt, "Plain body text here.")

    def test_a_topic_without_a_body_excerpts_its_description(self) -> None:
        (self.memory / "bare.md").write_text(_topic("bare", "Only a description").split("\n\nBody.")[0] + "\n")
        hits = rank_topics(self.workspace, "description")
        self.assertEqual((hits[0].excerpt, hits[0].line), ("Only a description", 1))

    def test_long_paragraphs_are_truncated_to_the_excerpt_limit(self) -> None:
        self.write_topic("long", "uv notes", body="uv " + "word " * 200)
        excerpt = rank_topics(self.workspace, "uv")[0].excerpt
        self.assertEqual(len(excerpt), memory_search.EXCERPT_MAX_CHARS)
        self.assertTrue(excerpt.endswith("…"))

    def test_keywords_are_indexed(self) -> None:
        self.write_topic("compact", "A short rule", body="Short.", extra="keywords: set -e, and-or list\n")
        self.assertEqual([hit.topic for hit in rank_topics(self.workspace, "and-or list")], ["compact"])

    def test_retired_topics_are_skipped_unless_asked_for(self) -> None:
        self.write_topic("old", "uv lesson", extra="status: retired\n")
        self.assertEqual(rank_topics(self.workspace, "uv"), [])
        hits = rank_topics(self.workspace, "uv", include_retired=True)
        self.assertEqual([(hit.topic, hit.status) for hit in hits], [("old", "retired")])

    def test_a_query_with_no_usable_tokens_or_an_empty_corpus_yields_nothing(self) -> None:
        self.assertEqual(rank_topics(self.workspace, "the and of"), [])
        self.write_topic("some", "uv lesson")
        self.assertEqual(rank_topics(self.workspace, "the and of"), [])
        self.assertEqual(rank_indexed([], "uv"), [])

    def test_an_empty_query_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            rank_topics(self.workspace, "   ")

    def test_malformed_and_unreadable_topics_are_skipped(self) -> None:
        (self.memory / "half.md").write_text("---\nname: half\n", encoding="utf-8")
        self.write_topic("good", "uv lesson")
        with unittest.mock.patch.object(memory_search, "read_text", side_effect=WorkspaceError("gone")):
            self.assertEqual(load_indexed_topics(self.workspace), [])
        self.assertEqual([item.topic.name for item in load_indexed_topics(self.workspace)], ["good"])

    def test_an_indexed_topic_document_carries_name_words_and_keywords(self) -> None:
        topic = MemoryTopic(path=Path("x"), name="git-forge", description="d", kind="method", scope="s", keywords="sha")
        self.assertEqual(IndexedTopic(topic=topic, body="b").document, "git forge\nd\ns\nsha\nb")


class PostmortemTests(SearchRootTestCase):
    def test_substring_hits_are_returned_with_line_numbers_and_hidden_dirs_are_skipped(self) -> None:
        project_dir = allocate_project(self.workspace, title="Cited", working_directory=self.target)
        (project_dir / "reflection.md").write_text("# R\n\nRun everything through UV here.\n", encoding="utf-8")
        (self.workspace / ".hidden").mkdir()
        (self.workspace / ".hidden" / "reflection.md").write_text("uv\n", encoding="utf-8")
        (self.workspace / "2026-01-01-001").mkdir()
        (self.workspace / "2026-01-01-002").mkdir()
        (self.workspace / "2026-01-01-002" / "reflection.md").write_bytes(b"\xff\xfe uv\n")
        hits = search_postmortems(self.workspace, "through uv")
        self.assertEqual(hits, [(project_dir / "reflection.md", 3, "Run everything through UV here.")])

    def test_an_empty_query_is_refused_and_an_absent_root_yields_nothing(self) -> None:
        with self.assertRaises(WorkspaceError):
            search_postmortems(self.workspace, " ")
        self.assertEqual(search_postmortems(self.root / "missing", "uv"), [])


class CreationGateTests(SearchRootTestCase):
    def test_an_existing_slug_or_an_explicit_create_passes(self) -> None:
        self.write_topic("uv-toolchain", "Run everything through uv")
        creation_gate(self.workspace, "uv-toolchain", "anything", create=False)
        creation_gate(self.workspace, "brand-new", "anything", create=True)

    def test_a_new_slug_is_refused_with_the_nearest_topics_listed(self) -> None:
        self.write_topic("uv-toolchain", "Run everything through uv", body="uv lock and uv sync.")
        self.write_topic("git-forge", "Read the sha that ran")
        self.assertEqual([hit.topic for hit in similar_topics(self.workspace, "uv sync")], ["uv-toolchain"])
        with self.assertRaises(WorkspaceError) as refused:
            creation_gate(self.workspace, "uv-lockfiles", "Always run uv sync after a lock change", create=False)
        message = str(refused.exception)
        self.assertIn("uv-toolchain — Run everything through uv", message)
        self.assertIn("--create", message)
        with self.assertRaises(WorkspaceError) as lonely:
            creation_gate(self.workspace, "zebra", "quantum chromodynamics", create=False)
        self.assertIn("no similar topic was found", str(lonely.exception))


class CandidateTests(SearchRootTestCase):
    def test_objective_text_reads_the_section_or_returns_empty(self) -> None:
        spec = "# T\n\n## Current specification\n\n### Objective and audience\n\nShip uv.\n\n### In scope\n\nx\n"
        self.assertEqual(objective_text(spec), "Ship uv.")
        self.assertEqual(objective_text("# T\n\nNothing.\n"), "")

    def test_candidates_are_bounded_in_count_and_bytes(self) -> None:
        for index in range(5):
            self.write_topic(f"topic-{index}", "uv " + "detail " * 60, body="uv lock " * 5)
        candidates = memory_candidates(self.workspace, title="uv lock", objective="")
        self.assertEqual(len(candidates), 3)
        self.assertLessEqual(len(json.dumps(candidates).encode("utf-8")), memory_search.CANDIDATE_MAX_BYTES)
        for item in candidates:
            self.assertLessEqual(len(str(item["description"])), memory_search.CANDIDATE_DESCRIPTION_CHARS)
        self.assertEqual(memory_candidates(self.workspace, title="uv lock", max_bytes=10), [])

    def test_no_memory_directory_or_a_failing_ranker_yields_no_candidates(self) -> None:
        self.assertEqual(memory_candidates(self.root / "elsewhere", title="uv"), [])
        self.write_topic("some", "uv lesson")
        with unittest.mock.patch.object(memory_search, "rank_indexed", side_effect=WorkspaceError("boom")):
            self.assertEqual(memory_candidates(self.workspace, title="uv"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
