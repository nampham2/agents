"""Tests for the cross-project memory layer.

The layer exists because one flat always-read file grew to 61 KB while its guardrail, which counted
entries rather than bytes, still reported it clean. So the tests that matter here are the ones that
hold the replacement honest about the same failure: the budget is measured on the file that is always
read, a malformed topic degrades to a skip rather than an exception, generation is derived rather than
hand-maintained, and two projects promoting at once lose nothing.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workspace_lib import (
    MEMORY_INDEX_MAX_BYTES,
    MEMORY_INDEX_MAX_LINES,
    MEMORY_STAGING_PLACEHOLDER,
    MemoryTopic,
    WorkspaceError,
    allocate_project,
    amend_memory_topic,
    load_memory_topics,
    memory_findings,
    memory_index_path,
    memory_staging_warnings,
    memory_topic_body,
    parse_memory_frontmatter,
    parse_memory_topic,
    rebuild_index,
    render_memory_index,
    render_memory_topic,
    search_memory,
    validate_project,
)


def _topic(
    name: str = "uv-toolchain",
    description: str = "Run everything through uv",
    kind: str = "environment",
    scope: str = "any repo with a uv.lock",
    sources: str = "2026-09-09-002",
    updated: str = "2026-09-09",
    body: str = "Bare pytest picks up the system interpreter.",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"kind: {kind}\n"
        f"scope: {scope}\n"
        f"sources: {sources}\n"
        f"updated: {updated}\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


class MemoryRootTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Resolved, because `allocate_project` returns a resolved path and macOS symlinks /var.
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.memory = self.workspace / "memory"
        self.memory.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def write_topic(self, slug: str, content: str | None = None, **kwargs: str) -> Path:
        path = self.memory / f"{slug}.md"
        path.write_text(content if content is not None else _topic(name=slug, **kwargs), encoding="utf-8")
        return path


class FrontmatterTests(unittest.TestCase):
    def test_a_well_formed_block_parses_into_its_six_fields(self) -> None:
        fields, problems = parse_memory_frontmatter(_topic())
        self.assertEqual(problems, [])
        self.assertEqual(
            fields,
            {
                "name": "uv-toolchain",
                "description": "Run everything through uv",
                "kind": "environment",
                "scope": "any repo with a uv.lock",
                "sources": "2026-09-09-002",
                "updated": "2026-09-09",
            },
        )

    def test_a_file_that_does_not_open_with_a_delimiter_is_rejected_whole(self) -> None:
        fields, problems = parse_memory_frontmatter("# Just a heading\n\nProse.\n")
        self.assertEqual(fields, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("must open with", problems[0])

    def test_an_empty_file_is_rejected_rather_than_indexed(self) -> None:
        fields, problems = parse_memory_frontmatter("")
        self.assertEqual(fields, {})
        self.assertIn("must open with", problems[0])

    def test_an_unclosed_block_is_reported(self) -> None:
        _, problems = parse_memory_frontmatter("---\nname: a\n")
        self.assertIn("never closed", " ".join(problems))

    def test_a_line_with_no_colon_is_reported_with_its_line_number(self) -> None:
        _, problems = parse_memory_frontmatter("---\nname: a\nnot a pair\n---\n")
        self.assertIn("line 3 is not a 'key: value' pair", " ".join(problems))

    def test_a_blank_line_inside_the_block_is_tolerated(self) -> None:
        fields, problems = parse_memory_frontmatter("---\nname: a\n\nkind: method\n---\n")
        self.assertEqual(problems, [])
        self.assertEqual(fields, {"name": "a", "kind": "method"})

    def test_a_duplicated_field_is_reported_rather_than_silently_last_wins(self) -> None:
        fields, problems = parse_memory_frontmatter("---\nname: a\nname: b\n---\n")
        self.assertIn("duplicate frontmatter field: 'name'", " ".join(problems))
        self.assertEqual(fields["name"], "b")


class TopicParsingTests(MemoryRootTestCase):
    def test_a_valid_topic_yields_a_topic_and_no_problems(self) -> None:
        path = self.write_topic("uv-toolchain")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        assert topic is not None
        self.assertEqual(topic.name, "uv-toolchain")
        self.assertEqual(topic.kind, "environment")
        self.assertEqual(topic.sources, ["2026-09-09-002"])

    def test_an_empty_sources_field_is_allowed_because_a_lesson_can_predate_its_projects(self) -> None:
        path = self.write_topic("uv-toolchain", sources="")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        assert topic is not None
        self.assertEqual(topic.sources, [])

    def test_an_empty_scope_is_not_allowed_because_it_is_how_a_reader_rules_a_lesson_out(self) -> None:
        path = self.write_topic("uv-toolchain", scope="")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("empty frontmatter field: 'scope'", " ".join(problems))

    def test_a_missing_field_is_named(self) -> None:
        path = self.write_topic("partial", content="---\nname: partial\nkind: method\n---\n\nBody.\n")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        joined = " ".join(problems)
        for field_name in ("description", "scope", "sources", "updated"):
            self.assertIn(f"missing frontmatter field: '{field_name}'", joined)

    def test_an_unknown_field_is_refused_like_every_other_schema_here(self) -> None:
        content = _topic(name="extra").replace("updated: 2026-09-09\n", "updated: 2026-09-09\noops: 1\n")
        path = self.write_topic("extra", content=content)
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("unknown frontmatter field(s): 'oops'", " ".join(problems))

    def test_a_name_that_disagrees_with_the_filename_is_refused(self) -> None:
        path = self.write_topic("on-disk", content=_topic(name="in-frontmatter"))
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("does not match the filename slug", " ".join(problems))

    def test_a_filename_that_is_not_a_slug_is_refused(self) -> None:
        path = self.write_topic("Not_A_Slug", content=_topic(name="Not_A_Slug"))
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("is not a lowercase-hyphenated slug", " ".join(problems))

    def test_a_kind_outside_the_closed_set_is_refused(self) -> None:
        path = self.write_topic("philosophical", kind="philosophy")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("is not one of: preference, environment, method", " ".join(problems))

    def test_a_non_iso_date_is_refused(self) -> None:
        path = self.write_topic("undated", updated="9 September")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("is not a YYYY-MM-DD date", " ".join(problems))

    def test_a_source_that_is_not_a_project_id_is_refused(self) -> None:
        path = self.write_topic("misattributed", sources="last tuesday")
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertIsNone(topic)
        self.assertIn("is not a YYYY-MM-DD-NNN project id", " ".join(problems))

    def test_several_sources_are_split_and_trimmed(self) -> None:
        path = self.write_topic("multi", sources=" 2026-09-01-001 ,2026-09-02-002 ")
        topic, _ = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        assert topic is not None
        self.assertEqual(topic.sources, ["2026-09-01-001", "2026-09-02-002"])


class TopicLoadingTests(MemoryRootTestCase):
    def test_an_absent_memory_directory_yields_nothing_and_no_problem(self) -> None:
        self.memory.rmdir()
        topics, problems = load_memory_topics(self.workspace)
        self.assertEqual((topics, problems), ([], []))

    def test_non_markdown_files_and_subdirectories_are_ignored(self) -> None:
        (self.memory / "notes.txt").write_text("not a topic", encoding="utf-8")
        (self.memory / "nested").mkdir()
        self.write_topic("uv-toolchain")
        topics, problems = load_memory_topics(self.workspace)
        self.assertEqual([topic.name for topic in topics], ["uv-toolchain"])
        self.assertEqual(problems, [])

    def test_a_malformed_file_is_skipped_while_its_neighbours_still_load(self) -> None:
        self.write_topic("uv-toolchain")
        self.write_topic("half-written", content="---\nname: half-written\n")
        topics, problems = load_memory_topics(self.workspace)
        self.assertEqual([topic.name for topic in topics], ["uv-toolchain"])
        self.assertTrue(all("half-written" in problem for problem in problems))

    def test_an_undecodable_file_is_a_problem_and_not_an_exception(self) -> None:
        (self.memory / "binary.md").write_bytes(b"---\nname: binary\n\xff\xfe\n")
        topics, problems = load_memory_topics(self.workspace)
        self.assertEqual(topics, [])
        self.assertIn("cannot read", " ".join(problems))


class IndexGenerationTests(MemoryRootTestCase):
    def test_pointers_are_grouped_by_kind_in_the_declared_order(self) -> None:
        self.write_topic("terse-prose", kind="preference", description="Prefer plain sentences")
        self.write_topic("uv-toolchain", kind="environment")
        self.write_topic("grill-rounds", kind="method", description="Ask the whole frontier")
        rendered = render_memory_index(self.workspace)
        self.assertLess(rendered.index("Confirmed user preferences"), rendered.index("Environment and tooling"))
        self.assertLess(rendered.index("Environment and tooling"), rendered.index("Method"))

    def test_an_empty_kind_renders_no_heading(self) -> None:
        self.write_topic("uv-toolchain", kind="environment")
        rendered = render_memory_index(self.workspace)
        self.assertNotIn("Confirmed user preferences", rendered)
        self.assertNotIn("Method", rendered)

    def test_a_pointer_carries_the_description_and_the_scope(self) -> None:
        self.write_topic("uv-toolchain")
        rendered = render_memory_index(self.workspace)
        self.assertIn(
            "- [uv-toolchain](memory/uv-toolchain.md) — Run everything through uv "
            "_(scope: any repo with a uv.lock)_",
            rendered,
        )

    def test_generation_is_idempotent(self) -> None:
        self.write_topic("uv-toolchain")
        self.assertEqual(render_memory_index(self.workspace), render_memory_index(self.workspace))

    def test_a_malformed_topic_contributes_no_pointer_and_raises_nothing(self) -> None:
        self.write_topic("half-written", content="---\nname: half-written\n")
        rendered = render_memory_index(self.workspace)
        self.assertNotIn("half-written", rendered)

    def test_post_mortems_are_titled_from_canonical_state_not_from_their_headings(self) -> None:
        project_dir = allocate_project(self.workspace, title="The canonical title", working_directory=self.target)
        (project_dir / "reflection.md").write_text("# Some other heading entirely\n\nProse.\n", encoding="utf-8")
        rendered = render_memory_index(self.workspace)
        self.assertIn(f"- [{project_dir.name}]({project_dir.name}/reflection.md) — The canonical title", rendered)
        self.assertNotIn("Some other heading", rendered)

    def test_a_project_without_a_post_mortem_gets_no_pointer(self) -> None:
        allocate_project(self.workspace, title="Unreflected", working_directory=self.target)
        self.assertNotIn("Project post-mortems", render_memory_index(self.workspace))

    def test_an_empty_post_mortem_gets_no_pointer(self) -> None:
        project_dir = allocate_project(self.workspace, title="Blank", working_directory=self.target)
        (project_dir / "reflection.md").write_text("   \n", encoding="utf-8")
        self.assertNotIn("Project post-mortems", render_memory_index(self.workspace))

    def test_a_post_mortem_without_canonical_state_gets_no_pointer(self) -> None:
        stray = self.workspace / "2026-01-01-001"
        stray.mkdir()
        (stray / "reflection.md").write_text("# Orphan\n\nProse.\n", encoding="utf-8")
        self.assertNotIn("Project post-mortems", render_memory_index(self.workspace))

    def test_an_unreadable_project_json_gets_no_pointer(self) -> None:
        stray = self.workspace / "2026-01-01-002"
        stray.mkdir()
        (stray / "reflection.md").write_text("# Orphan\n\nProse.\n", encoding="utf-8")
        (stray / "project.json").write_text("{not json", encoding="utf-8")
        self.assertNotIn("Project post-mortems", render_memory_index(self.workspace))

    def test_a_pipe_in_a_title_is_escaped_so_the_pointer_stays_one_line(self) -> None:
        project_dir = allocate_project(self.workspace, title="A | B", working_directory=self.target)
        (project_dir / "reflection.md").write_text("# R\n\nProse.\n", encoding="utf-8")
        self.assertIn("A \\| B", render_memory_index(self.workspace))

    def test_hidden_directories_and_plain_files_are_skipped(self) -> None:
        (self.workspace / ".hidden").mkdir()
        (self.workspace / "loose.md").write_text("not a project", encoding="utf-8")
        self.assertNotIn("Project post-mortems", render_memory_index(self.workspace))

    def test_an_absent_workspace_root_renders_a_header_and_nothing_else(self) -> None:
        rendered = render_memory_index(self.root / "does-not-exist")
        self.assertIn("# Cross-project memory", rendered)
        self.assertNotIn("Project post-mortems", rendered)


class RebuildIndexTests(MemoryRootTestCase):
    def test_rebuilding_writes_both_derived_files_and_returns_the_index_path(self) -> None:
        self.write_topic("uv-toolchain")
        returned = rebuild_index(self.workspace)
        self.assertEqual(returned.name, "INDEX.md")
        self.assertIn("uv-toolchain", memory_index_path(self.workspace).read_text(encoding="utf-8"))

    def test_a_root_with_no_memory_layer_at_all_gets_no_memory_index(self) -> None:
        self.memory.rmdir()
        rebuild_index(self.workspace)
        self.assertFalse(memory_index_path(self.workspace).exists())

    def test_an_existing_memory_index_is_refreshed_even_without_the_directory(self) -> None:
        self.memory.rmdir()
        memory_index_path(self.workspace).write_text("# Stale\n", encoding="utf-8")
        rebuild_index(self.workspace)
        self.assertNotIn("Stale", memory_index_path(self.workspace).read_text(encoding="utf-8"))

    def test_a_malformed_topic_does_not_stop_the_rebuild(self) -> None:
        self.write_topic("half-written", content="---\nname: half-written\n")
        rebuild_index(self.workspace)
        self.assertTrue(memory_index_path(self.workspace).is_file())


class TopicRenderingTests(unittest.TestCase):
    def test_a_rendered_topic_round_trips_through_the_parser(self) -> None:
        topic = MemoryTopic(
            path=Path("memory/lock-discipline.md"),
            name="lock-discipline",
            description="How the workspace locks compose",
            kind="method",
            scope="workspace_lib writers",
            sources=["2026-09-09-002"],
            updated="2026-09-09",
        )
        rendered = render_memory_topic(topic, "Locks are per-directory mkdir, not reentrant.")
        parsed, problems = parse_memory_topic(Path("memory/lock-discipline.md"), rendered)
        self.assertEqual(problems, [])
        assert parsed is not None
        self.assertEqual(parsed.sources, ["2026-09-09-002"])
        self.assertEqual(memory_topic_body(rendered), "Locks are per-directory mkdir, not reentrant.")

    def test_a_body_with_no_frontmatter_is_returned_whole(self) -> None:
        self.assertEqual(memory_topic_body("\nJust prose.\n"), "Just prose.")

    def test_an_unclosed_frontmatter_yields_an_empty_body(self) -> None:
        self.assertEqual(memory_topic_body("---\nname: a\n"), "")


class AmendTopicTests(MemoryRootTestCase):
    def test_a_new_topic_is_created_from_the_supplied_metadata(self) -> None:
        path = amend_memory_topic(
            self.workspace,
            "lock-discipline",
            body="Locks are not reentrant.",
            description="How the workspace locks compose",
            kind="method",
            scope="workspace_lib writers",
            sources=["2026-09-09-002"],
            updated="2026-09-09",
        )
        topic, problems = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        assert topic is not None
        self.assertEqual(topic.kind, "method")

    def test_a_new_topic_without_a_description_kind_or_scope_is_refused_naming_all_three(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(self.workspace, "bare", body="Something.")
        self.assertIn("needs description, kind, scope", str(caught.exception))

    def test_amending_appends_below_the_existing_body_rather_than_replacing_it(self) -> None:
        self.write_topic("uv-toolchain", body="The first lesson.")
        path = amend_memory_topic(self.workspace, "uv-toolchain", body="The second lesson.")
        content = path.read_text(encoding="utf-8")
        self.assertLess(content.index("The first lesson."), content.index("The second lesson."))

    def test_amending_merges_sources_without_duplicating_them(self) -> None:
        self.write_topic("uv-toolchain", sources="2026-09-01-001")
        path = amend_memory_topic(
            self.workspace,
            "uv-toolchain",
            body="More.",
            sources=["2026-09-01-001", "2026-09-02-002"],
        )
        topic, _ = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        assert topic is not None
        self.assertEqual(topic.sources, ["2026-09-01-001", "2026-09-02-002"])

    def test_amending_overrides_only_the_metadata_it_is_given(self) -> None:
        self.write_topic("uv-toolchain")
        path = amend_memory_topic(self.workspace, "uv-toolchain", body="More.", scope="every repo")
        topic, _ = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        assert topic is not None
        self.assertEqual(topic.scope, "every repo")
        self.assertEqual(topic.description, "Run everything through uv")
        self.assertEqual(topic.kind, "environment")

    def test_amending_with_a_new_description_and_kind_replaces_them(self) -> None:
        self.write_topic("uv-toolchain")
        path = amend_memory_topic(
            self.workspace,
            "uv-toolchain",
            body="More.",
            description="A sharper description",
            kind="method",
        )
        topic, _ = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        assert topic is not None
        self.assertEqual(topic.description, "A sharper description")
        self.assertEqual(topic.kind, "method")

    def test_updated_defaults_to_today_rather_than_being_left_stale(self) -> None:
        self.write_topic("uv-toolchain", updated="2020-01-01")
        path = amend_memory_topic(self.workspace, "uv-toolchain", body="More.")
        topic, _ = parse_memory_topic(path, path.read_text(encoding="utf-8"))
        assert topic is not None
        self.assertNotEqual(topic.updated, "2020-01-01")

    def test_a_non_slug_name_is_refused_before_any_lock_is_taken(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(self.workspace, "Not A Slug", body="x", description="d", kind="method", scope="s")
        self.assertIn("lowercase-hyphenated", str(caught.exception))
        self.assertFalse((self.workspace / ".memory.lock").exists())

    def test_a_kind_outside_the_closed_set_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(self.workspace, "topic", body="x", description="d", kind="philosophy", scope="s")
        self.assertIn("is not one of", str(caught.exception))

    def test_a_source_that_is_not_a_project_id_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(
                self.workspace, "topic", body="x", description="d", kind="method", scope="s", sources=["nope"]
            )
        self.assertIn("project id", str(caught.exception))

    def test_a_non_iso_updated_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(
                self.workspace, "topic", body="x", description="d", kind="method", scope="s", updated="yesterday"
            )
        self.assertIn("YYYY-MM-DD", str(caught.exception))

    def test_an_empty_body_is_refused_because_an_amendment_with_nothing_in_it_is_a_no_op(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(self.workspace, "topic", body="   \n", description="d", kind="method", scope="s")
        self.assertIn("non-empty body", str(caught.exception))

    def test_an_unparseable_existing_topic_is_refused_rather_than_silently_rewritten(self) -> None:
        self.write_topic("half-written", content="---\nname: half-written\n")
        with self.assertRaises(WorkspaceError) as caught:
            amend_memory_topic(self.workspace, "half-written", body="More.")
        self.assertIn("refusing to amend an unparseable topic file", str(caught.exception))
        # The file is left exactly as it was: a rewrite here would destroy whatever it does hold.
        self.assertEqual((self.memory / "half-written.md").read_text(encoding="utf-8"), "---\nname: half-written\n")

    def test_a_filesystem_failure_becomes_a_workspace_error(self) -> None:
        with unittest.mock.patch("workspace_lib.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(WorkspaceError) as caught:
                amend_memory_topic(
                    self.workspace, "topic", body="x", description="d", kind="method", scope="s"
                )
        self.assertIn("cannot amend", str(caught.exception))

    def test_two_concurrent_promotions_into_one_topic_lose_nothing(self) -> None:
        amend_memory_topic(
            self.workspace,
            "lock-discipline",
            body="The original lesson.",
            description="How the workspace locks compose",
            kind="method",
            scope="workspace_lib writers",
            updated="2026-09-09",
        )

        def promote(index: int) -> None:
            amend_memory_topic(
                self.workspace,
                "lock-discipline",
                body=f"Concurrent lesson {index}.",
                sources=[f"2026-09-0{index}-00{index}"],
                updated="2026-09-09",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(promote, (1, 2)))

        path = self.memory / "lock-discipline.md"
        content = path.read_text(encoding="utf-8")
        # Both bodies and both source ids survive. Without the lock the later read-modify-write starts
        # from a copy of the file that predates the earlier one, and the loss is silent.
        self.assertIn("The original lesson.", content)
        self.assertIn("Concurrent lesson 1.", content)
        self.assertIn("Concurrent lesson 2.", content)
        topic, problems = parse_memory_topic(path, content)
        self.assertEqual(problems, [])
        assert topic is not None
        self.assertEqual(sorted(topic.sources), ["2026-09-01-001", "2026-09-02-002"])

    def test_the_memory_lock_is_released_before_the_call_returns(self) -> None:
        amend_memory_topic(self.workspace, "topic", body="x", description="d", kind="method", scope="s")
        # Held across a rebuild, this lock would sit inside `.index.lock` and the two mkdir locks
        # would be nested for as long as generation took.
        self.assertFalse((self.workspace / ".memory.lock").exists())


class StagingWarningTests(MemoryRootTestCase):
    def test_an_absent_staging_file_is_silent(self) -> None:
        project_dir = self.workspace / "2026-01-01-001"
        project_dir.mkdir()
        self.assertEqual(memory_staging_warnings(project_dir), [])

    def test_the_untouched_skeleton_is_silent(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        self.assertEqual(memory_staging_warnings(project_dir), [])

    def test_a_staged_line_is_warned_about(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        staging = project_dir / "memory-staging.md"
        staging.write_text(
            staging.read_text(encoding="utf-8").replace(MEMORY_STAGING_PLACEHOLDER, "- Something surprising."),
            encoding="utf-8",
        )
        warnings = memory_staging_warnings(project_dir)
        self.assertEqual(len(warnings), 1)
        self.assertIn("still holds staged lessons", warnings[0])

    def test_headings_alone_are_not_staged_content(self) -> None:
        project_dir = self.workspace / "2026-01-01-002"
        project_dir.mkdir()
        (project_dir / "memory-staging.md").write_text("# Staged lessons\n\n## Later\n", encoding="utf-8")
        self.assertEqual(memory_staging_warnings(project_dir), [])

    def test_an_unreadable_staging_file_is_silent_rather_than_raising(self) -> None:
        project_dir = self.workspace / "2026-01-01-003"
        project_dir.mkdir()
        (project_dir / "memory-staging.md").write_bytes(b"\xff\xfe staged")
        self.assertEqual(memory_staging_warnings(project_dir), [])

    def test_the_warning_reaches_close_validation(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        (project_dir / "memory-staging.md").write_text("- Something surprising.\n", encoding="utf-8")
        report = validate_project(project_dir, close=True)
        self.assertEqual(len([w for w in report.warnings if "still holds staged lessons" in w]), 1)

    def test_the_warning_does_not_fire_before_close(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        (project_dir / "memory-staging.md").write_text("- Something surprising.\n", encoding="utf-8")
        report = validate_project(project_dir)
        self.assertEqual([w for w in report.warnings if "still holds staged lessons" in w], [])


class MemoryFindingsTests(MemoryRootTestCase):
    def test_a_root_with_no_memory_layer_produces_no_findings(self) -> None:
        self.memory.rmdir()
        report = memory_findings(self.workspace)
        self.assertEqual((report.errors, report.warnings), ([], []))

    def test_a_legacy_flat_reflection_warns_that_it_is_legacy(self) -> None:
        (self.workspace / "reflection.md").write_text("# Cross-project reflection\n", encoding="utf-8")
        report = memory_findings(self.workspace)
        self.assertEqual(len([w for w in report.warnings if "legacy flat cross-project" in w]), 1)

    def test_a_malformed_topic_is_an_error_at_validation_though_only_a_skip_at_generation(self) -> None:
        self.write_topic("half-written", content="---\nname: half-written\n")
        self.assertTrue(memory_findings(self.workspace).errors)
        self.assertNotIn("half-written", render_memory_index(self.workspace))

    def test_an_over_long_index_is_an_error_naming_the_line_budget_only(self) -> None:
        body = "".join(f"- [t{i}](memory/t{i}.md) — p\n" for i in range(MEMORY_INDEX_MAX_LINES + 1))
        memory_index_path(self.workspace).write_text(f"# Cross-project memory\n{body}", encoding="utf-8")
        errors = [error for error in memory_findings(self.workspace).errors if "above the" in error]
        self.assertEqual(len(errors), 1)
        self.assertIn(f"above the {MEMORY_INDEX_MAX_LINES} allowed", errors[0])
        self.assertNotIn("bytes", errors[0])

    def test_an_over_large_index_is_an_error_naming_the_byte_budget_only(self) -> None:
        # One line, well over the byte budget: the two bounds are independent, which is the whole
        # point of not counting entries.
        memory_index_path(self.workspace).write_text("x" * (MEMORY_INDEX_MAX_BYTES + 1), encoding="utf-8")
        errors = [error for error in memory_findings(self.workspace).errors if "above the" in error]
        self.assertEqual(len(errors), 1)
        self.assertIn(f"above the {MEMORY_INDEX_MAX_BYTES} allowed", errors[0])
        self.assertNotIn("lines", errors[0])

    def test_an_index_within_budget_is_not_an_error(self) -> None:
        self.write_topic("uv-toolchain")
        memory_index_path(self.workspace).write_text(render_memory_index(self.workspace), encoding="utf-8")
        self.assertEqual([e for e in memory_findings(self.workspace).errors if "above the" in e], [])

    def test_an_unreadable_index_is_an_error(self) -> None:
        memory_index_path(self.workspace).write_bytes(b"\xff\xfe")
        self.assertIn("unreadable", " ".join(memory_findings(self.workspace).errors))

    def test_a_source_naming_no_project_in_the_workspace_warns(self) -> None:
        self.write_topic("uv-toolchain", sources="2026-01-01-009")
        warnings = [w for w in memory_findings(self.workspace).warnings if "cites source projects" in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-01-01-009", warnings[0])

    def test_a_source_naming_a_present_project_does_not_warn(self) -> None:
        project_dir = allocate_project(self.workspace, title="Cited", working_directory=self.target)
        self.write_topic("uv-toolchain", sources=project_dir.name)
        self.assertEqual([w for w in memory_findings(self.workspace).warnings if "cites source" in w], [])

    def test_check_index_catches_an_index_that_disagrees_with_regeneration(self) -> None:
        self.write_topic("uv-toolchain")
        memory_index_path(self.workspace).write_text("# Cross-project memory\n", encoding="utf-8")
        self.assertIn("is stale", " ".join(memory_findings(self.workspace, check_index=True).errors))

    def test_check_index_catches_a_missing_index_when_topics_exist(self) -> None:
        self.write_topic("uv-toolchain")
        self.assertIn("is stale", " ".join(memory_findings(self.workspace, check_index=True).errors))

    def test_check_index_catches_an_unreadable_index(self) -> None:
        self.write_topic("uv-toolchain")
        memory_index_path(self.workspace).write_bytes(b"\xff\xfe")
        self.assertIn("is stale", " ".join(memory_findings(self.workspace, check_index=True).errors))

    def test_a_regenerated_index_satisfies_check_index(self) -> None:
        self.write_topic("uv-toolchain")
        rebuild_index(self.workspace)
        self.assertEqual(memory_findings(self.workspace, check_index=True).errors, [])

    def test_findings_reach_project_validation(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        self.write_topic("half-written", content="---\nname: half-written\n")
        report = validate_project(project_dir)
        self.assertFalse(report.valid)
        self.assertIn("half-written", " ".join(report.errors))

    def test_a_malformed_topic_never_blocks_a_commit(self) -> None:
        project_dir = allocate_project(self.workspace, title="Fresh", working_directory=self.target)
        self.write_topic("half-written", content="---\nname: half-written\n")
        # The commit path validates the candidate state and then rebuilds; neither reads a topic
        # file as anything but advisory, so finished work is still recordable.
        rebuild_index(self.workspace)
        self.assertTrue((project_dir / "project.json").is_file())
        self.assertTrue(memory_index_path(self.workspace).is_file())


class SearchTests(MemoryRootTestCase):
    def test_an_empty_query_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            search_memory(self.workspace, "   ")

    def test_a_frontmatter_hit_is_returned_with_its_line_number(self) -> None:
        path = self.write_topic("uv-toolchain")
        hits = search_memory(self.workspace, "everything through uv")
        self.assertEqual(hits, [(path, 3, "description: Run everything through uv")])

    def test_matching_is_case_insensitive(self) -> None:
        self.write_topic("uv-toolchain")
        self.assertTrue(search_memory(self.workspace, "RUN EVERYTHING"))

    def test_a_topic_body_is_not_searched_because_pointers_are_how_bodies_are_reached(self) -> None:
        self.write_topic("uv-toolchain", body="A distinctive phrase in the body.")
        self.assertEqual(search_memory(self.workspace, "distinctive phrase"), [])

    def test_a_topic_with_no_frontmatter_is_searched_as_a_single_line(self) -> None:
        self.write_topic("loose", content="Just a line mentioning uv.\n")
        self.assertTrue(search_memory(self.workspace, "mentioning uv"))

    def test_an_unclosed_frontmatter_searches_only_its_first_line(self) -> None:
        self.write_topic("half-written", content="---\nname: half-written\nkind: method\n")
        self.assertEqual(search_memory(self.workspace, "half-written"), [])

    def test_post_mortem_bodies_are_searched_and_come_after_frontmatter(self) -> None:
        topic_path = self.write_topic("uv-toolchain")
        project_dir = allocate_project(self.workspace, title="Cited", working_directory=self.target)
        (project_dir / "reflection.md").write_text("# R\n\nRun everything through uv here too.\n", encoding="utf-8")
        hits = search_memory(self.workspace, "everything through uv")
        self.assertEqual([hit[0] for hit in hits], [topic_path, project_dir / "reflection.md"])

    def test_an_unreadable_topic_and_post_mortem_are_skipped_rather_than_raising(self) -> None:
        (self.memory / "binary.md").write_bytes(b"---\nname: binary\n\xff\xfe uv\n")
        project_dir = self.workspace / "2026-01-01-001"
        project_dir.mkdir()
        (project_dir / "reflection.md").write_bytes(b"\xff\xfe uv\n")
        self.assertEqual(search_memory(self.workspace, "uv"), [])

    def test_hidden_directories_and_projects_without_post_mortems_are_skipped(self) -> None:
        (self.workspace / ".hidden").mkdir()
        (self.workspace / ".hidden" / "reflection.md").write_text("uv\n", encoding="utf-8")
        (self.workspace / "loose.md").write_text("uv\n", encoding="utf-8")
        (self.workspace / "2026-01-01-001").mkdir()
        self.assertEqual(search_memory(self.workspace, "uv"), [])

    def test_an_absent_workspace_root_yields_no_hits(self) -> None:
        self.assertEqual(search_memory(self.root / "does-not-exist", "uv"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
