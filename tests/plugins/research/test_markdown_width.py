"""The shipped skill documents are wrapped at 100 columns, and nothing enforced it.

About thirty over-width prose lines had accumulated across the project skill's documents, most from
one commit that compressed the entry document, because the convention lived in reviewers' memories
rather than in a check. Ruff's 120-column rule covers Python only. This test is the Markdown
counterpart: prose lines in the shipped skill documents stay within `WIDTH` columns.

Fenced code blocks, table rows, and lines carrying a URL are exempt, because breaking any of them
changes what a reader copies. `parallel-execution.md` is exempt as a whole: it is the executor
protocol, authored at a wider column with hundreds of such lines, and reflowing it is not this
test's business. Everything else under `plugins/` is checked, and the layout sweep is asserted to
find documents so an empty glob cannot pass vacuously.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.conftest import REPO_ROOT

PLUGINS_DIR = REPO_ROOT / "plugins"
WIDTH = 100
EXEMPT_DOCUMENTS = frozenset({"parallel-execution.md"})
URL_PATTERN = re.compile(r"https?://")


def over_width_lines(text: str, *, width: int = WIDTH) -> "list[int]":
    """1-based numbers of prose lines longer than `width`, skipping fences, tables and URLs."""
    offending: "list[int]" = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("|") or URL_PATTERN.search(line):
            continue
        if len(line) > width:
            offending.append(number)
    return offending


def checked_documents(plugins_dir: Path) -> "list[Path]":
    return sorted(path for path in plugins_dir.rglob("*.md") if path.name not in EXEMPT_DOCUMENTS)


class MarkdownWidthTests(unittest.TestCase):
    def test_shipped_documents_stay_within_width(self) -> None:
        problems = []
        for document in checked_documents(PLUGINS_DIR):
            relative = document.relative_to(PLUGINS_DIR)
            for number in over_width_lines(document.read_text(encoding="utf-8")):
                problems.append(f"{relative}:{number}")
        self.assertEqual([], problems)

    def test_the_sweep_finds_the_skill_documents(self) -> None:
        names = {document.name for document in checked_documents(PLUGINS_DIR)}
        self.assertIn("SKILL.md", names)
        self.assertIn("commands.md", names)
        self.assertNotIn("parallel-execution.md", names)

    def test_the_check_has_teeth(self) -> None:
        long_line = "word " * 30
        text = "\n".join([
            "# Title",
            long_line,
            "```",
            long_line,
            "```",
            "| " + long_line + " |",
            "see " + long_line + " https://example.com/x",
            "short line",
        ])
        self.assertEqual([2], over_width_lines(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
