"""Shared pytest configuration.

The code under test ships as plugin scripts that Claude invokes directly with
`python3 <skill-dir>/scripts/...`, not as an installed package, so tests import
them the same way the skill does: as top-level modules on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/research/skills/project/scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
