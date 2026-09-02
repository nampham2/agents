"""Keep every published plugin version aligned with the repository release version."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = (
    "plugins/research/.claude-plugin/plugin.json",
    "plugins/research/.codex-plugin/plugin.json",
    "plugins/research/.kimi-plugin/plugin.json",
    "kimi.plugin.json",
)


class PluginVersionTests(unittest.TestCase):
    """The project version is canonical for every install surface and the lock file."""

    def test_every_release_version_matches_pyproject(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        canonical = project["project"]["version"]
        self.assertRegex(canonical, re.compile(r"^\d+\.\d+\.\d+$"))

        observed = {
            relative: json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))["version"]
            for relative in MANIFESTS
        }
        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = [
            package["version"]
            for package in lock["package"]
            if package["name"] == project["project"]["name"]
        ]

        self.assertEqual([canonical], locked)
        self.assertEqual({canonical}, set(observed.values()), observed)

    def test_root_kimi_manifest_exposes_the_research_skills(self) -> None:
        manifest = json.loads((REPO_ROOT / "kimi.plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("research", manifest["name"])
        self.assertEqual("./plugins/research/skills", manifest["skills"])

    def test_readme_leads_with_direct_github_installation_for_every_host(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        direct_install = readme.index("## Install directly from GitHub (recommended)")
        local_development = readme.index("## Development from a clone")

        self.assertLess(direct_install, local_development)
        self.assertIn("claude plugin marketplace add nampham2/agents", readme)
        self.assertIn("codex plugin marketplace add nampham2/agents --ref main", readme)
        self.assertIn("/plugins install https://github.com/nampham2/agents/tree/main", readme)


if __name__ == "__main__":
    unittest.main()
