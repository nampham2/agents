"""Consistency tests between what the shipped skill documents say and what the tools require.

Two kinds of drift have already happened in this plugin and neither was caught by anything: the
grill skill told users to invoke it as `/grill` when a plugin skill is only reachable as
`/<plugin>:<skill>`, and the specification headings grill wrote were never compared with the
headings the validator looks for. Both are contracts between two files, so both belong in a test
rather than in a reviewer's memory.

A third followed from hiding `grill`: the frontmatter reader below only matched `[a-z_]+` keys, so a
hyphenated key like `user-invocable` was not merely ignored, it was folded into the preceding key's
value. `name: grill` followed by `user-invocable: false` parsed as the name
`'grill user-invocable: false'`. A skill that is deliberately not user-invocable must also not claim
an invocation users cannot type, so the invocation rule is conditional on that key rather than
unconditional.

The checks are functions over a directory of plugins, so the same code runs against the real tree
and against a deliberately broken fixture. A consistency test that cannot be made to fail proves
nothing.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from workspace_lib import SPEC_CANONICAL_SECTIONS, spec_section_warnings

from tests.conftest import REPO_ROOT

PLUGINS_DIR = REPO_ROOT / "plugins"
FRONTMATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
INVOCATION_PATTERN = re.compile(r"^Invoke with `(?P<invocation>/[^`\s]+)", re.MULTILINE)
FENCED_MARKDOWN_PATTERN = re.compile(r"```markdown\n(.*?)^\s*```", re.DOTALL | re.MULTILINE)


def skill_documents(plugins_dir: Path) -> "list[Path]":
    """Every shipped SKILL.md, discovered from the layout rather than from a hand-kept list."""
    return sorted(plugins_dir.glob("*/skills/*/SKILL.md"))


def parse_frontmatter(text: str) -> "dict[str, str]":
    """Read the top-level scalar keys of a skill's YAML frontmatter.

    Deliberately not a YAML parser: the repository ships no runtime dependencies, and the only
    structure asserted here is `key: value` with optional folded continuation lines. Keys may contain
    hyphens: `user-invocable` and `allowed-tools` are real skill frontmatter fields, and a reader
    that cannot see them silently appends them to the value above instead.
    """
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        return {}
    fields: "dict[str, str]" = {}
    key: "str | None" = None
    for line in match.group(1).splitlines():
        header = re.match(r"^([a-z][a-z0-9_-]*):\s*(.*)$", line)
        if header:
            key = header.group(1)
            value = header.group(2).strip()
            fields[key] = "" if value == ">" else value
            continue
        if key is not None and line.strip():
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    return fields


def documented_spec_sections(text: str) -> "list[str]":
    """The `###` headings a skill document lists inside a fenced markdown block."""
    sections: "list[str]" = []
    for block in FENCED_MARKDOWN_PATTERN.findall(text):
        dedented = "\n".join(line.strip() for line in block.splitlines())
        sections.extend(re.findall(r"^### (.+)$", dedented, re.MULTILINE))
    return sections


def documentation_problems(plugins_dir: Path) -> "list[str]":
    """Every inconsistency found across the shipped skill documents, as readable sentences."""
    problems: "list[str]" = []
    required = [name for name, _ in SPEC_CANONICAL_SECTIONS]

    for document in skill_documents(plugins_dir):
        skill_name = document.parent.name
        plugin_name = document.parents[2].name
        expected = f"/{plugin_name}:{skill_name}"
        relative = document.relative_to(plugins_dir)
        text = document.read_text(encoding="utf-8")

        fields = parse_frontmatter(text)
        if not fields:
            problems.append(f"{relative} has no parseable frontmatter")
            continue
        for required_field in ("name", "description"):
            if not fields.get(required_field):
                problems.append(f"{relative} frontmatter has no non-empty '{required_field}'")
        if fields.get("name") not in (None, skill_name):
            problems.append(f"{relative} frontmatter name {fields['name']!r} is not its directory name {skill_name!r}")

        # `user-invocable: false` hides the slash command from users while leaving the skill
        # available to the model, so such a skill must not tell anyone to type one.
        user_invocable = fields.get("user-invocable", "true").strip().lower() != "false"
        invocation = INVOCATION_PATTERN.search(text)
        if not user_invocable:
            if invocation:
                problems.append(
                    f"{relative} is not user-invocable but says "
                    f"'Invoke with `{invocation.group('invocation')}`'"
                )
        elif not invocation:
            problems.append(f"{relative} has no 'Invoke with `/...`' line")
        elif not invocation.group("invocation").startswith(expected):
            problems.append(f"{relative} says {invocation.group('invocation')!r}, not {expected!r}")

        # A bare `/<skill>` does not resolve for a plugin skill, and it is exactly what the
        # invocation line said before this was checked.
        for bare in re.finditer(rf"(?<!:)\B/{re.escape(skill_name)}\b", text):
            line = text[: bare.start()].count("\n") + 1
            problems.append(f"{relative}:{line} names a bare /{skill_name} instead of {expected}")

        documented = documented_spec_sections(text)
        if documented and documented != required:
            problems.append(f"{relative} documents specification sections {documented}, tools require {required}")
        elif documented:
            spec = "# T\n\n## Current specification\n\n" + "".join(f"### {name}\n\nx\n\n" for name in documented)
            warnings = spec_section_warnings(spec)
            if warnings:
                problems.append(f"{relative} documents sections the validator still warns about: {warnings}")

    return problems


class ShippedSkillDocumentTests(unittest.TestCase):
    def test_every_shipped_skill_document_is_consistent(self) -> None:
        self.assertEqual([], documentation_problems(PLUGINS_DIR))

    def test_the_layout_actually_yields_documents(self) -> None:
        # An empty sweep would make every assertion above vacuously true.
        found = {document.parent.name for document in skill_documents(PLUGINS_DIR)}
        self.assertIn("project", found)
        self.assertIn("grill", found)

    def test_both_skills_document_the_specification_headings(self) -> None:
        required = [name for name, _ in SPEC_CANONICAL_SECTIONS]
        for document in skill_documents(PLUGINS_DIR):
            with self.subTest(skill=document.parent.name):
                self.assertEqual(required, documented_spec_sections(document.read_text(encoding="utf-8")))


class BrokenFixtureTests(unittest.TestCase):
    """The same checks over deliberately wrong documents, so the assertions are known to have teeth."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugins = Path(self.tmp.name)
        self.skill_dir = self.plugins / "demo" / "skills" / "widget"
        self.skill_dir.mkdir(parents=True)

    def _write(self, body: str) -> "list[str]":
        (self.skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        return documentation_problems(self.plugins)

    def _document(
        self,
        *,
        invocation: "str | None" = "/demo:widget",
        name: str = "widget",
        sections: "list[str] | None" = None,
        user_invocable: "bool | None" = None,
    ) -> str:
        listing = "".join(f"### {section}\n" for section in (sections or [name for name, _ in SPEC_CANONICAL_SECTIONS]))
        flag = "" if user_invocable is None else f"user-invocable: {str(user_invocable).lower()}\n"
        invoke = "" if invocation is None else f"Invoke with `{invocation} [subject]`.\n\n"
        return (
            "---\n"
            f"name: {name}\n"
            f"{flag}"
            "description: >\n"
            "  Use when a fixture is needed.\n"
            "---\n"
            "\n"
            "# Widget\n"
            "\n"
            f"{invoke}"
            "```markdown\n"
            f"{listing}"
            "```\n"
        )

    def test_a_correct_fixture_reports_nothing(self) -> None:
        self.assertEqual([], self._write(self._document()))

    def test_a_bare_invocation_is_caught(self) -> None:
        problems = self._write(self._document(invocation="/widget"))
        self.assertTrue(any("/demo:widget" in problem for problem in problems), problems)

    def test_a_bare_mention_elsewhere_is_caught(self) -> None:
        problems = self._write(self._document() + "\nRun `/widget` whenever you like.\n")
        self.assertTrue(any("bare /widget" in problem for problem in problems), problems)

    def test_a_missing_invocation_line_is_caught(self) -> None:
        body = self._document().replace("Invoke with `/demo:widget [subject]`.", "Just start talking.")
        self.assertIn("demo/skills/widget/SKILL.md has no 'Invoke with `/...`' line", self._write(body))

    def test_missing_frontmatter_is_caught(self) -> None:
        body = self._document().split("---\n", 2)[-1]
        self.assertIn("demo/skills/widget/SKILL.md has no parseable frontmatter", self._write(body))

    def test_an_empty_description_is_caught(self) -> None:
        body = self._document().replace("description: >\n  Use when a fixture is needed.\n", "description:\n")
        problems = self._write(body)
        self.assertIn("demo/skills/widget/SKILL.md frontmatter has no non-empty 'description'", problems)

    def test_a_frontmatter_name_that_is_not_the_directory_is_caught(self) -> None:
        body = self._document(name="gadget").replace("Invoke with `/demo:gadget", "Invoke with `/demo:widget")
        problems = self._write(body)
        self.assertTrue(any("is not its directory name" in problem for problem in problems), problems)

    def test_a_renamed_specification_section_is_caught(self) -> None:
        sections = [name for name, _ in SPEC_CANONICAL_SECTIONS]
        sections[0] = "Goal and readers"
        problems = self._write(self._document(sections=sections))
        self.assertTrue(any("documents specification sections" in problem for problem in problems), problems)

    def test_a_reordered_specification_section_list_is_caught(self) -> None:
        sections = list(reversed([name for name, _ in SPEC_CANONICAL_SECTIONS]))
        problems = self._write(self._document(sections=sections))
        self.assertTrue(any("documents specification sections" in problem for problem in problems), problems)

    def test_a_hidden_skill_that_still_claims_an_invocation_is_caught(self) -> None:
        problems = self._write(self._document(user_invocable=False))
        self.assertIn(
            "demo/skills/widget/SKILL.md is not user-invocable but says 'Invoke with `/demo:widget`'",
            problems,
        )

    def test_a_hidden_skill_with_no_invocation_line_reports_nothing(self) -> None:
        # The inverse of the rule above has to hold, or hiding a skill would be impossible.
        self.assertEqual([], self._write(self._document(invocation=None, user_invocable=False)))

    def test_a_visible_skill_still_needs_its_invocation_line(self) -> None:
        problems = self._write(self._document(invocation=None, user_invocable=True))
        self.assertIn("demo/skills/widget/SKILL.md has no 'Invoke with `/...`' line", problems)

    def test_a_hyphenated_key_does_not_corrupt_the_key_above_it(self) -> None:
        # The bug this guards: `[a-z_]+` did not match `user-invocable`, so the line was treated as a
        # folded continuation of `name` and the parsed name became "widget user-invocable: false".
        fields = parse_frontmatter(self._document(invocation=None, user_invocable=False))
        self.assertEqual("widget", fields["name"])
        self.assertEqual("false", fields["user-invocable"])

    def test_a_document_without_a_section_listing_is_not_forced_to_have_one(self) -> None:
        # Only a skill that documents the headings has to document them correctly; a future skill
        # with nothing to say about spec.md is not in breach.
        body = self._document().split("```markdown")[0]
        self.assertEqual([], self._write(body))


if __name__ == "__main__":
    unittest.main()
