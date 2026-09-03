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

    def test_kimi_marketplace_publishes_the_nested_research_plugin(self) -> None:
        self.assertFalse((REPO_ROOT / "kimi.plugin.json").exists())
        self.assertFalse((REPO_ROOT / ".kimi-plugin/plugin.json").exists())

        marketplace_path = REPO_ROOT / ".kimi-plugin/marketplace.json"
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        research = next(plugin for plugin in marketplace["plugins"] if plugin["id"] == "research")
        plugin_root = (marketplace_path.parent / research["source"]).resolve()

        self.assertEqual("2", marketplace["version"])
        self.assertEqual(REPO_ROOT / "plugins/research", plugin_root)
        self.assertTrue((plugin_root / ".kimi-plugin/plugin.json").is_file())

    def test_readme_leads_with_marketplace_installation_for_every_host(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        marketplace_install = readme.index("## Install through a marketplace")
        local_development = readme.index("## Development from a clone")

        self.assertLess(marketplace_install, local_development)
        self.assertIn("claude plugin marketplace add nampham2/agents", readme)
        self.assertIn("codex plugin marketplace add nampham2/agents --ref main", readme)
        self.assertIn("/plugins marketplace /absolute/path/to/agents/.kimi-plugin/marketplace.json", readme)
        self.assertNotIn("/plugins install https://github.com/nampham2/agents", readme)


if __name__ == "__main__":
    unittest.main()
