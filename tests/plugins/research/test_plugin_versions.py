"""Keep every published plugin version aligned with the repository release version."""

from __future__ import annotations

import json
import os
import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = (
    "plugins/research/.claude-plugin/plugin.json",
    "plugins/research/.codex-plugin/plugin.json",
)

# A project that edits the plugin works on one alpha line: the release version with an alpha number
# that only goes up. Python files spell it 0.24.0a3 (PEP 440) and plugin manifests spell it
# 0.24.0-alpha.3 (SemVer). Both normalize to the Python spelling, so one value must remain.
PEP440 = re.compile(r"^\d+\.\d+\.\d+(a\d+)?$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-alpha\.\d+)?$")
RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


def normalize(version: str) -> str:
    """Return the PEP 440 spelling of a plain or alpha version in either spelling."""
    return re.sub(r"-alpha\.(\d+)$", r"a\1", version)


class PluginVersionTests(unittest.TestCase):
    """The project version is canonical for every install surface and the lock file."""

    def test_every_release_version_matches_pyproject(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        canonical = project["project"]["version"]
        self.assertRegex(canonical, PEP440)

        observed = {
            relative: json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))["version"]
            for relative in MANIFESTS
        }
        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = [package["version"] for package in lock["package"] if package["name"] == project["project"]["name"]]

        for version in observed.values():
            self.assertRegex(version, SEMVER)
        self.assertEqual([canonical], locked)
        self.assertEqual({canonical}, {normalize(version) for version in observed.values()}, observed)

    def test_normalize_maps_both_spellings_to_one_value(self) -> None:
        self.assertEqual("0.24.0a3", normalize("0.24.0-alpha.3"))
        self.assertEqual("0.24.0a3", normalize("0.24.0a3"))
        self.assertEqual("0.24.0", normalize("0.24.0"))
        self.assertNotEqual(normalize("0.24.0-alpha.3"), normalize("0.24.0-alpha.4"))

    @unittest.skipUnless(
        os.environ.get("REQUIRE_RELEASE_VERSION") == "1",
        "set REQUIRE_RELEASE_VERSION=1 to require a plain release version",
    )
    def test_no_alpha_version_reaches_main(self) -> None:
        """CI sets the variable for a PR to main and for a push to main. An alpha line stays on branches."""
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertRegex(project["project"]["version"], RELEASE)
        for relative in MANIFESTS:
            version = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))["version"]
            self.assertRegex(version, RELEASE, relative)

    def test_readme_leads_with_marketplace_installation_for_every_host(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        marketplace_install = readme.index("## Install through a marketplace")
        local_development = readme.index("## Development from a clone")

        self.assertLess(marketplace_install, local_development)
        self.assertIn("claude plugin marketplace add nampham2/agents", readme)
        self.assertIn("codex plugin marketplace add nampham2/agents --ref main", readme)
        self.assertNotIn("/plugins install https://github.com/nampham2/agents", readme)


if __name__ == "__main__":
    unittest.main()
