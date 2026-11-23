"""The coding standard reference: one pointer in the entry, a self-contained short file, the persona link.

The benchmark appends the whole reference to a headless worker's prompt, so the file must stand alone
and stay short: every word is paid for in every session.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import REPO_ROOT

SKILL_DIR = REPO_ROOT / "plugins/research/skills/project"
ENTRY = SKILL_DIR / "SKILL.md"
STANDARD = SKILL_DIR / "references/coding-standard.md"
PERSONA = SKILL_DIR / "references/worker-personas.md"
WORD_CEILING = 600


def flat(path: Path) -> str:
    """The file with whitespace collapsed, so a re-wrap does not break a phrase match."""
    return " ".join(path.read_text(encoding="utf-8").split())


def pointer_paragraphs() -> list[str]:
    """Paragraphs of the entry that name the standard, whitespace collapsed."""
    paragraphs = ENTRY.read_text(encoding="utf-8").split("\n\n")
    return [" ".join(paragraph.split()) for paragraph in paragraphs if "coding-standard" in paragraph]


def test_the_entry_has_exactly_one_pointer_to_the_standard() -> None:
    pointers = pointer_paragraphs()
    assert len(pointers) == 1, pointers
    assert pointers[0].count("coding-standard") == 2  # the link text and its target
    assert "(references/coding-standard.md)" in pointers[0]
    assert len(pointers[0].split()) <= 8  # a pointer, not a second copy of the rules


def test_the_pointer_makes_the_read_precede_editing_on_a_code_task() -> None:
    (pointer,) = pointer_paragraphs()
    pointer = pointer.lower()
    assert "code task" in pointer
    assert "read [coding-standard.md]" in pointer
    assert "before editing" in pointer


def test_the_standard_exists_and_is_not_empty() -> None:
    assert STANDARD.is_file()
    assert len(flat(STANDARD).split()) > 200  # an empty file would pass every absence check below


def test_the_standard_stays_under_its_word_ceiling() -> None:
    words = len(flat(STANDARD).split())
    assert words <= WORD_CEILING, words


def test_the_standard_is_self_contained() -> None:
    text = STANDARD.read_text(encoding="utf-8")
    assert "](" not in text, "relative link"
    assert re.search(r"\b[\w./-]+\.(md|py|json|sh|toml|txt)\b", text) is None, "names a file"
    for marker in ("references/", "/Users/", "/home/", "~/", "research-project", "SKILL"):
        assert marker not in text, marker


def test_the_standard_carries_the_failing_first_instruction() -> None:
    text = flat(STANDARD)
    assert "Write the test first and run it against the unchanged code" in text
    assert "fails for the right reason" in text


def test_the_standard_carries_the_mutation_testing_instruction() -> None:
    text = flat(STANDARD)
    assert "When a mutation testing tool" in text
    assert "run it on the functions you touched" in text
    assert "kill the surviving mutants" in text
    assert "equivalent" in text
    assert "mutation result" in text


def test_the_standard_carries_the_gate_integrity_and_honesty_rules() -> None:
    text = flat(STANDARD)
    assert "cyclomatic complexity of at most 10" in text
    assert "at least 90%" in text
    assert "casts, ignore comments, skipped tests, weakened assertions or lowered thresholds" in text
    assert "what you did not verify" in text


def test_the_persona_points_to_the_standard_by_name() -> None:
    text = flat(PERSONA)
    assert "[coding-standard.md](coding-standard.md)" in text
