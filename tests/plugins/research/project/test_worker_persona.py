"""The coding worker persona: the reference file and the packet key `persona` (T07, C8).

The private repository and user names are not checked here: a shipped test would have to carry them
or a guessable digest of them. They are checked by a recorded workspace command (design section
10.5). This file asserts only patterns that name nothing private.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from workspace_packets import build_packet

from tests.plugins.research.project.test_packets import HEADING, _set
from tests.plugins.research.project.test_packets import _project as _packet_project

ROOT = Path(__file__).resolve().parents[4]
PERSONA = ROOT / "plugins" / "research" / "skills" / "project" / "references" / "worker-personas.md"
CODE_OUTPUTS = [{"root": "target", "path": "src/tool.py", "required": True}]
EFFECT = {"kind": "local_write", "description": "edit files in the target"}
HEADINGS = (
    "# Coding worker",
    "## Before you edit",
    "## While you edit",
    "## Before you hand back",
    "## Your report",
)


def _text() -> str:
    return PERSONA.read_text(encoding="utf-8")


def _flat() -> str:
    """The file with whitespace collapsed, so a re-wrap does not break a sentence match."""
    return " ".join(_text().split())


def _packet(project: Path, task_id: str = "T02") -> dict[str, Any]:
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    task = next(item for item in state["tasks"] if item["id"] == task_id)
    return build_packet(project, state, task)


# --- the file ---------------------------------------------------------------------------------------


def test_the_persona_file_exists_and_names_the_worker_contract() -> None:
    assert PERSONA.is_file()
    flat = _flat()
    assert "The worker instructions still apply." in flat
    assert "task-workers.md" in flat


def test_the_persona_has_the_five_sections() -> None:
    lines = _text().splitlines()
    found = [line for line in lines if line.startswith("#")]
    assert found == list(HEADINGS)


def test_the_persona_tells_the_worker_to_read_the_repository_instructions() -> None:
    # Design section 10.6 gives the sentence without code marks; the file may mark the two names.
    assert "Read the repository's AGENTS.md or CLAUDE.md before you edit." in _flat().replace("`", "")


def test_the_persona_holds_no_project_id_and_no_home_path() -> None:
    text = _text()
    assert text.strip()  # an empty file would pass every search below
    assert re.search(r"\d{4}-\d{2}-\d{2}-\d{3}", text) is None
    for marker in ("/Users/", "/home/", "~/"):
        assert marker not in text, marker


def test_the_persona_states_test_first_with_its_two_exceptions() -> None:
    flat = _flat()
    assert "fails for the right reason" in flat
    assert "pure deletion or refactor" in flat
    assert "deliberately wrong input" in flat


# --- the packet key ---------------------------------------------------------------------------------


def test_a_code_task_packet_names_the_persona_by_absolute_path(tmp_path: Path) -> None:
    project = _packet_project(tmp_path, 6)
    _set(project, "T02", spec_refs=[HEADING], effect=EFFECT, outputs=CODE_OUTPUTS)
    packet = _packet(project)
    assert "persona" in packet
    named = Path(packet["persona"])
    assert named.is_absolute()
    assert named == PERSONA.resolve()
    assert named.is_file()
    assert packet["result_format"] == "references/task-workers.md"


def test_another_task_packet_names_no_persona(tmp_path: Path) -> None:
    project = _packet_project(tmp_path, 6)
    prose = [{"root": "target", "path": "docs/guide.md", "required": True}]
    workspace = [{"root": "workspace", "path": "artifacts/tool.py", "required": True}]
    cases: list[dict[str, Any]] = [
        {"effect": EFFECT, "outputs": prose},
        {"effect": EFFECT, "outputs": workspace},
        {"effect": {"kind": "read_only", "description": "read"}, "outputs": CODE_OUTPUTS},
        {"effect": None, "outputs": []},
    ]
    for fields in cases:
        _set(project, "T02", spec_refs=[HEADING], **fields)
        packet = _packet(project)
        assert "persona" in packet, fields
        assert packet["persona"] is None, fields


def test_the_persona_key_does_not_change_the_packet_digest(tmp_path: Path) -> None:
    project = _packet_project(tmp_path, 6)
    _set(project, "T02", spec_refs=[HEADING], effect=EFFECT, outputs=CODE_OUTPUTS)
    code = _packet(project)
    _set(project, "T02", outputs=[])
    other = _packet(project)
    assert code["persona"] is not None and other["persona"] is None
    assert code["packet_sha256"] == other["packet_sha256"]


def test_a_policy_five_packet_has_no_persona_key(tmp_path: Path) -> None:
    project = _packet_project(tmp_path, 5)
    _set(project, "T02", spec_refs=[HEADING], effect=EFFECT, outputs=CODE_OUTPUTS)
    assert "persona" not in _packet(project)
