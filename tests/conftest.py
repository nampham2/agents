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


# The alignment gates are enforced only for projects created on or after a cutoff date. Every test
# project is created "now", so once that date is in the past the whole suite would be gated. The
# cutoff is therefore held in the far future for every test, and the tests of the gate itself move
# it into the past for the projects they mean to gate. A project exercised through a subprocess is
# beyond a monkeypatch; `backdate_project` edits its `created` on disk instead.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def far_future_gate_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import workspace_lib

    # raising=False: a build without the constant (the baseline a mutation check runs against) must
    # be left alone, or every test would error in setup there and the check would prove nothing.
    monkeypatch.setattr(workspace_lib, "GATES_ENFORCED_FROM", "2099-01-01T00:00:00+00:00", raising=False)


@pytest.fixture
def current_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make every project created in the test a current one: the gate cutoff moves into the past.

    The counterpart of the autouse fixture above, for the tests of behaviour that only current
    projects have. Move it after a project reaches PLANNING if the test must not be gated on the way.
    A launcher subprocess reads the production cutoff, so a project it creates now is already current.
    """
    import workspace_lib

    cutoff = "2020-01-01T00:00:00+00:00"
    monkeypatch.setattr(workspace_lib, "GATES_ENFORCED_FROM", cutoff, raising=False)
    return cutoff


def backdate_project(project_dir: Path, created: str = "2020-01-01T00:00:00+00:00") -> None:
    """Make a project legacy for a launcher that reads the production cutoff: rewrite `created` on disk."""
    import json

    path = project_dir / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["created"] = created
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
