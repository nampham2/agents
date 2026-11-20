"""The supported hosts are Claude Code and Pi; the removed host leaves no trace outside docs/."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
# Built from two parts so that this file does not name the removed host itself.
REMOVED = "co" + "dex"


def tracked_files() -> list[str]:
    """List the tracked files, or fail under CI and skip elsewhere when Git cannot."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        message = f"git ls-files is unavailable: {error}"
        if os.environ.get("CI"):
            raise AssertionError(message) from error
        raise unittest.SkipTest(message) from error
    return [name for name in completed.stdout.decode("utf-8").split("\0") if name]


class HostContractTests(unittest.TestCase):
    """AGENTS.md names two hosts, and nothing tracked names the removed one."""

    def test_agents_md_names_claude_code_and_pi_as_the_supported_hosts(self) -> None:
        text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        section = text[text.index("## Cross-host plugin contract") :]
        rows = [
            line
            for line in section.split("\n- ", 1)[0].splitlines()
            if line.startswith("|") and not line.startswith(("| Host", "| ---"))
        ]
        self.assertEqual(
            [row.split("|")[1].strip() for row in rows],
            ["Claude Code", "Pi"],
        )

    def test_the_removed_host_files_are_absent(self) -> None:
        self.assertFalse((REPO_ROOT / "plugins" / "research" / f".{REMOVED}-plugin").exists())
        self.assertFalse((REPO_ROOT / ".agents" / "plugins" / "marketplace.json").exists())

    def test_no_tracked_file_outside_docs_names_the_removed_host(self) -> None:
        pattern = re.compile(REMOVED, re.IGNORECASE)
        hits: dict[str, list[str]] = {}
        for name in tracked_files():
            if name.startswith("docs/"):
                continue
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            lines = [line for line in text.splitlines() if pattern.search(line)]
            if lines:
                hits[name] = lines

        others = sorted(name for name in hits if name != "README.md")
        self.assertEqual(others, [], "files that name the removed host")
        readme = hits.get("README.md", [])
        self.assertLessEqual(len(readme), 1)
        if readme:
            text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
            releases = text.index("## Releases")
            self.assertGreater(text.index(readme[0]), releases)


if __name__ == "__main__":
    unittest.main()
