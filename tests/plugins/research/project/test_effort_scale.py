"""The project skill rates effort on a three-level scale, never in time units."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
REFERENCE = REPO_ROOT / "plugins/research/skills/project/references/architecture-review.md"
TIME_UNITS = re.compile(r"\b(hours?|days?|weeks?|engineer-days?|person-days?)\b", re.IGNORECASE)


def effort_section() -> str:
    text = REFERENCE.read_text(encoding="utf-8")
    start = text.index("## Make effort reviewable")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_effort_section_names_all_three_levels() -> None:
    section = effort_section()
    for level in ("Low:", "Medium:", "High:"):
        assert level in section, f"{level} is not defined in the effort section"
    assert "low, medium or high" in section


def test_effort_section_forbids_time_units_as_estimates() -> None:
    """The only time words allowed are the ones in the sentence that forbids them."""
    section = effort_section()
    prohibition = "Never estimate in hours, days or other time units"
    assert prohibition in section
    remainder = section.replace(prohibition, "")
    assert TIME_UNITS.search(remainder) is None, TIME_UNITS.search(remainder)
    assert "ranges and units" not in REFERENCE.read_text(encoding="utf-8")
