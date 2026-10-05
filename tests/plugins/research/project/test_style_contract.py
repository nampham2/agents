"""Contracts between the style code and the writing-rules documents.

The preferred-word table lives twice: as `PREFERRED_WORDS` in the code, which the check reads, and as a
table in `writing-rules-extra.md`, which a reader sees. Prose is read once and forgotten, so the two
copies are compared here instead of being trusted to stay equal. The documents also follow their own
rules: a rules file that the check flags would teach the wrong lesson.
"""

from __future__ import annotations

import re

import pytest
from workspace_style import PREFERRED_WORDS, document_findings

from tests.conftest import REPO_ROOT

REFERENCES = REPO_ROOT / "plugins/research/skills/project/references"
EXTRA = REFERENCES / "writing-rules-extra.md"
ROW = re.compile(r"^\|\s*(?P<key>[^|]+?)\s*\|\s*(?P<value>[^|]+?)\s*\|\s*$")


def table_of(markdown: str, heading: str) -> dict[str, str]:
    """The two-column table under `## heading`, as {instead_of: write}; the header and rule rows are skipped."""
    start = re.search(rf"^##\s+{re.escape(heading)}\s*$", markdown, re.MULTILINE)
    assert start, f"no '## {heading}' section"
    rest = markdown[start.end() :]
    end = re.search(r"^##\s+", rest, re.MULTILINE)
    rows: dict[str, str] = {}
    for line in rest[: end.start() if end else None].splitlines():
        match = ROW.match(line)
        if match and not set(match["key"]) <= set("-: ") and match["key"] != "Instead of":
            assert match["key"] not in rows, f"duplicate row {match['key']!r}"
            rows[match["key"]] = match["value"]
    return rows


def test_the_table_in_the_document_equals_the_table_in_the_code() -> None:
    documented = table_of(EXTRA.read_text(encoding="utf-8"), "Preferred words")
    assert documented == PREFERRED_WORDS
    assert len(documented) > 50  # an empty parse would make the equality above vacuous


def test_the_table_parser_sees_a_changed_row_and_ignores_the_header() -> None:
    header = "## Preferred words\n\n| Instead of | Write |\n| --- | --- |\n"
    text = header + "| utilize | use |\n| prior to | before |\n\n## Next\n"
    assert table_of(text, "Preferred words") == {"utilize": "use", "prior to": "before"}
    assert table_of(text.replace("| use |", "| apply |"), "Preferred words") != {"utilize": "use", "prior to": "before"}


def test_the_parser_refuses_a_missing_section_and_a_duplicate_row() -> None:
    with pytest.raises(AssertionError, match="no '## Preferred words' section"):
        table_of("# Title\n", "Preferred words")
    with pytest.raises(AssertionError, match="duplicate row"):
        table_of("## Preferred words\n\n| a | b |\n| a | c |\n", "Preferred words")


def test_the_extra_document_follows_its_own_rules() -> None:
    assert [str(finding) for finding in document_findings(EXTRA.read_text(encoding="utf-8"))] == []


def test_the_extra_document_says_that_its_list_is_incomplete_and_claims_no_compliance() -> None:
    text = EXTRA.read_text(encoding="utf-8")
    assert "## This list is incomplete" in text
    assert "do not claim that\nproject records follow ASD-STE100" in text


RULES = REFERENCES / "writing-rules.md"
RULE_LINE = re.compile(r"^(?P<number>\d+)\. \((?P<mark>[DI])\) ", re.MULTILINE)
GROUPS = {
    "Words": 10,
    "Noun clusters": 3,
    "Verbs": 8,
    "Sentences": 8,
    "Procedures": 6,
    "Descriptions": 6,
    "Punctuation and layout": 4,
}


def glossary_terms(markdown: str) -> list[str]:
    start = markdown.index("## Glossary")
    return re.findall(r"^- \*\*(.+?)\*\*: .+\.$", markdown[start:], re.MULTILINE)


def rules_in_group(markdown: str, group: str) -> int:
    start = re.search(rf"^## {re.escape(group)}\s*$", markdown, re.MULTILINE)
    assert start, f"no '## {group}' group"
    rest = markdown[start.end() :]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return len(RULE_LINE.findall(rest[: end.start() if end else None]))


def test_there_are_45_numbered_rules_marked_documented_or_inspired() -> None:
    found = RULE_LINE.findall(RULES.read_text(encoding="utf-8"))
    assert [int(number) for number, _ in found] == list(range(1, 46))
    marks = [mark for _, mark in found]
    assert (marks.count("D"), marks.count("I")) == (13, 32)


def test_the_counts_in_the_text_match_the_rules() -> None:
    text = RULES.read_text(encoding="utf-8")
    assert "with these 45 rules" in text
    assert "Thirteen come from the public list" in text
    assert "The other 32 are marked (I)" in text


def test_each_group_has_its_planned_number_of_rules() -> None:
    text = RULES.read_text(encoding="utf-8")
    assert {group: rules_in_group(text, group) for group in GROUPS} == GROUPS


def test_the_rules_document_follows_its_own_rules() -> None:
    assert [str(finding) for finding in document_findings(RULES.read_text(encoding="utf-8"))] == []


def test_no_glossary_term_is_a_preferred_word_key_and_terms_are_unique() -> None:
    terms = glossary_terms(RULES.read_text(encoding="utf-8"))
    assert len(terms) >= 8
    assert len(terms) == len(set(terms))
    assert {term.lower() for term in terms} & set(PREFERRED_WORDS) == set()


def test_the_glossary_parser_and_the_overlap_check_see_a_bad_term() -> None:
    text = "## Glossary\n\n- **verify**: to check a thing.\n- **gate**: a rule.\n"
    assert glossary_terms(text) == ["verify", "gate"]
    assert {term.lower() for term in glossary_terms(text)} & set(PREFERRED_WORDS) == {"verify"}


def test_the_rules_document_links_to_the_word_table_and_claims_no_compliance() -> None:
    text = RULES.read_text(encoding="utf-8")
    assert "(writing-rules-extra.md)" in text
    assert EXTRA.is_file()
    assert "Do not claim that a record follows STE." in text


def test_the_group_counter_refuses_a_missing_group() -> None:
    with pytest.raises(AssertionError, match="no '## Verbs' group"):
        rules_in_group("# T\n", "Verbs")
