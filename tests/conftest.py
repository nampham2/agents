"""Shared pytest configuration.

The code under test ships as plugin scripts that Claude invokes directly with
`python3 <skill-dir>/scripts/...`, not as an installed package, so tests import
them the same way the skill does: as top-level modules on sys.path.

The script locations live here and nowhere else. Test modules import them from
this module rather than recomputing them: three modules previously each spelled
out the same relative path, and a directory rename left the whole suite red
because every copy had to be found and repointed by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / "plugins/research/skills/project/scripts"
MANAGER = SKILL_SCRIPTS / "manage_workspace.py"
VALIDATOR = SKILL_SCRIPTS / "validate_workspace.py"

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
