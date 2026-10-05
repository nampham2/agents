"""Unit tests for the advisory style findings.

The real-record fixtures are the F2 section and the T02 task fields of project 2026-10-02-002,
copied with their original line wrapping. A first version of the check split sentences per line and
reported nothing on a 39-word sentence, so the wrapped records are the test that matters most.
"""

from __future__ import annotations

import time

from workspace_style import (
    LIMIT_STEP,
    LIMIT_TEXT,
    MAX_FINDINGS,
    PREFERRED_WORDS,
    Finding,
    _clean,
    _sentences,
    _words,
    document_findings,
    format_warning,
    section_only,
    task_field_findings,
)

F2_BEFORE = """### F2: terminal acceptance on every entry point

New `newly_terminal_task_errors(project_dir, previous, candidate) -> list[str]` in
`workspace_lib.py`, called from `commit_candidate` next to the gate, for each task whose status is
`DONE` in `candidate` and was not `DONE` in `previous` (Rule C1). For each evidence reference of
such a task that points at `workspace:evidence.md` with an anchor, it resolves the anchor through
`workspace_evidence.evidence_verdicts()` and requires: the record exists exactly once, is
selectable (well-formed metadata), is owned by this task, and passed. A failed, foreign or
ambiguous record is an error naming the record ID and the verdict. A task finishing on
observation-only references through `update` is refused with the instruction to use
`task finish --observation`, because only that path writes the attested-finish finding first
(`workspace_operations.py:126-134`). At least one reference must be a resolvable record.

Compatibility: an anchor that matches no generated record (legacy anchors such as a bare task ID,
as `tests/plugins/research/project/test_lifecycle_payloads.py` writes) is an error for gated
projects and a warning for pre-cutoff projects. Already-`DONE` tasks are never re-read.
`task finish` keeps its own earlier check for the clearer error; the commit-level check is the
one that cannot be bypassed.
"""

F2_AFTER = """### F2: terminal acceptance on every entry point

Add `newly_terminal_task_errors(project_dir, previous, candidate)` to `workspace_lib.py`. It returns
a list of error strings. `commit_candidate` calls it next to the gate (Rule C1). The function checks
each task that is `DONE` in `candidate` and was not `DONE` in `previous`.

The function checks each evidence reference of such a task. The reference must point at
`workspace:evidence.md` with an anchor. The function resolves the anchor with
`workspace_evidence.evidence_verdicts()`. The function requires four conditions:

- The record exists exactly once.
- The record is selectable (the metadata is well formed).
- This task owns the record.
- The record passed.

A failed, foreign or ambiguous record is an error. The error names the record ID and the verdict.

An update can finish a task with observation-only references. The function refuses this update. The
error tells the user to run `task finish --observation`. Only that command writes the attested-finish
finding first (`workspace_operations.py:126-134`). At least one reference must be a resolvable record.

Compatibility: some legacy anchors match no generated record, for example a bare task ID.
`tests/plugins/research/project/test_lifecycle_payloads.py` writes such anchors. In a gated project,
such an anchor is an error. In a project from before the cutoff, it is a warning. The function never
reads a task that is already `DONE` again.

`task finish` keeps its own earlier check, because that error is clearer. The commit-level check
no caller can bypass.
"""

T02_BEFORE = (
    "A revision-checked update that sets a task DONE with a failed, foreign, ambiguous or "
    "observation-only record is refused with the record and verdict named; already-DONE tasks are never "
    "re-judged; legacy anchors warn for pre-cutoff projects and error for gated ones; task finish still works.",
    "Equivalence tests for update versus task finish on success, failure, retry, repaired state and legacy "
    "fixtures, failing on HEAD for the right reason first.",
)

T02_AFTER = (
    "Refuse an update that sets a task to DONE with a failed, foreign, ambiguous or observation-only record. "
    "The refusal names the record and the verdict. Do not judge a task that is already DONE again. A legacy "
    "anchor gives a warning in a project from before the cutoff. It gives an error in a gated project. "
    "`task finish` still works.",
    "Write equivalence tests for `update` and `task finish`. Cover success, failure, retry, repaired state and "
    "legacy fixtures. Run the tests on HEAD first. Each test must fail for the expected reason. Run them again "
    "after the change.",
)


def words(count: int) -> str:
    return " ".join(["word"] * count)


def rules(markdown: str, **options: int) -> list[str]:
    return [finding.rule for finding in document_findings(markdown, **options)]


def test_real_wrapped_record_before_the_rewrite_has_the_long_sentences() -> None:
    found = document_findings(F2_BEFORE)
    assert [finding.rule for finding in found] == ["length"] * 4
    assert [finding.message for finding in found] == [
        f"sentence of {count} words (limit 25)" for count in (28, 39, 26, 31)
    ]
    assert [finding.where for finding in found] == ["L3", "L5", "L9", "L14"]


def test_real_wrapped_record_after_the_rewrite_has_none() -> None:
    assert document_findings(F2_AFTER) == []


def test_real_task_fields_before_and_after() -> None:
    def tasks(criteria: str, verification: str) -> list[dict[str, str]]:
        return [{"id": "T02", "name": "Enforce acceptance", "success_criteria": criteria, "verification": verification}]

    before = task_field_findings(tasks(*T02_BEFORE))
    assert [(finding.where, finding.rule) for finding in before] == [
        ("T02 success_criteria", "length"),
        ("T02 verification", "length"),
    ]
    assert task_field_findings(tasks(*T02_AFTER)) == []


def test_a_sentence_that_wraps_over_lines_counts_once() -> None:
    found = document_findings("Start\n" + words(30) + "\nend.\n")
    assert found == [Finding("L1", "length", "sentence of 32 words (limit 25)")]


def test_length_limit_is_25_and_numbered_items_use_it_unless_a_step_limit_is_given() -> None:
    assert "length" not in rules(words(LIMIT_TEXT) + ".")
    assert rules(words(LIMIT_TEXT + 1) + ".") == ["length"]
    assert "length" not in rules("- " + words(LIMIT_TEXT) + ".")
    assert rules("1. " + words(LIMIT_STEP + 1) + ".") == []  # a numbered list is a description by default
    assert rules("1. " + words(LIMIT_TEXT + 1) + ".") == ["length"]


def test_a_step_limit_applies_to_numbered_items_only() -> None:
    assert rules("1. " + words(LIMIT_STEP + 1) + ".", step_limit=LIMIT_STEP) == ["length"]
    assert rules("2) " + words(LIMIT_STEP + 1) + ".", step_limit=LIMIT_STEP) == ["length"]
    assert rules("1. " + words(LIMIT_STEP) + ".", step_limit=LIMIT_STEP) == []
    assert rules("- " + words(LIMIT_STEP + 1) + ".", step_limit=LIMIT_STEP) == []
    assert rules(words(LIMIT_STEP + 1) + ".", step_limit=LIMIT_STEP) == []


def test_an_explicit_limit_replaces_both_limits() -> None:
    assert rules(words(21) + ".", limit=20) == ["length"]
    assert rules("1. " + words(21) + ".", limit=30) == []


def test_symbols_do_not_count_as_words() -> None:
    assert _words("a — b -> c") == 3


def test_paragraph_over_six_sentences_is_found_once_at_its_first_line() -> None:
    seven = " ".join(["Do it."] * 7)
    assert document_findings("Intro.\n\n" + seven + "\n") == [
        Finding("L3", "paragraph", "paragraph of 7 sentences (limit 6)")
    ]
    assert document_findings(" ".join(["Do it."] * 6)) == []


def test_a_list_is_not_one_long_paragraph() -> None:
    assert document_findings("\n".join(["- Do it. Do it."] * 8)) == []


def test_list_item_continuation_lines_join_the_item() -> None:
    found = document_findings("- Start " + words(10) + "\n  " + words(16) + " end.\n")
    assert found == [Finding("L1", "length", "sentence of 28 words (limit 25)")]


def test_an_unindented_line_after_an_item_starts_a_new_paragraph() -> None:
    found = document_findings("- Short item.\n" + words(30) + ".\n")
    assert found == [Finding("L2", "length", "sentence of 30 words (limit 25)")]


def test_preferred_words_are_found_once_per_sentence_ignoring_case_and_wrapping() -> None:
    found = document_findings("Utilize the tool. Then UTILIZE it\nagain, prior\nto the end, and utilize it.")
    assert [str(finding) for finding in found] == [
        "L1 [word] 'utilize': write 'use'",
        "L1 [word] 'utilize': write 'use'",
        "L1 [word] 'prior to': write 'before'",
    ]


def test_every_preferred_word_is_found_by_the_pattern() -> None:
    for key, replacement in PREFERRED_WORDS.items():
        assert key == " ".join(key.lower().split())
        assert key != replacement
        found = document_findings(f"We {key.upper()} it.")
        assert any(finding.rule == "word" and finding.message.startswith(f"'{key}'") for finding in found), key


def test_contractions_are_found_and_possessives_are_not() -> None:
    found = document_findings("It isn\u2019t ready. We're late. They'd go. The tool's output is fine. I'm here.")
    assert [finding.message for finding in found] == [
        "'isn't': write the full form",
        "'We're': write the full form",
        "'They'd': write the full form",
        "'I'm': write the full form",
    ]


def test_three_verb_forms_are_found() -> None:
    found = document_findings("The tool will stop. You Should read. It could fail. It can fail.")
    assert [finding.message for finding in found] == [
        "'will': use the simple present or an imperative",
        "'should': use the simple present or an imperative",
        "'could': use the simple present or an imperative",
    ]


def test_exempt_text_is_not_checked() -> None:
    bad = "We will utilize " + words(40) + "."
    exempt = "\n".join(
        [
            "# Heading " + bad,
            "| " + bad + " |",
            "> " + bad,
            "---",
            "* * *",
            "<!-- " + bad + " -->",
            "<!--",
            bad,
            "-->",
            "```text",
            bad,
            "```",
            "~~~",
            bad,
            "~~~",
            "Run `" + bad + "` now.",
            'The user said "' + bad + '" once.',
            "Read [the file](https://example.com/" + "x" * 80 + ") now.",
            "Read the <b>file</b> now.",
        ]
    )
    assert document_findings(exempt) == []


def test_an_unclosed_fence_hides_the_rest_without_failing() -> None:
    assert document_findings("Fine.\n\n```\nWe will utilize " + words(40) + ".\n") == []


def test_a_comment_that_opens_and_closes_on_one_line_ends_a_block() -> None:
    assert document_findings("Fine <!-- c -->\n<!-- one line -->\nWe will go.\n") == [
        Finding("L3", "verb", "'will': use the simple present or an imperative")
    ]


def test_abbreviations_and_dotted_names_do_not_end_a_sentence() -> None:
    text = "Use a tool, e.g. a script, i.e. a file, etc. Read workspace_lib.py or v0.22.0 now."
    assert [sentence for _, sentence in _sentences(text)] == [text]
    assert [sentence for _, sentence in _sentences("One. Two! Three? Four")] == ["One.", "Two!", "Three?", "Four"]


def test_sentence_findings_point_at_the_line_where_the_sentence_starts() -> None:
    found = document_findings("Short one.\nThe tool will\nrun. Then it will stop.\n")
    assert [finding.where for finding in found] == ["L2", "L3"]


def test_emphasis_links_code_and_quotes_are_reduced_before_counting() -> None:
    assert _clean("**Bold** and _it_ [text](http://x) done.") == "Bold and it text done."
    assert _clean("Run `a b c` or \"x y\" or \u201cp q\u201d now") == "Run CODE or QUOTE or QUOTE now"
    assert _clean("It\u2019s here") == "It's here"
    assert _clean("snake_case_name stays") == "snake_case_name stays"


def test_blank_input_has_no_findings() -> None:
    assert document_findings("") == []
    assert document_findings("\n\n   \n") == []


def test_task_field_findings_skip_wrong_types_without_error() -> None:
    assert task_field_findings(None) == []
    assert task_field_findings({"id": "T01"}) == []
    assert task_field_findings(["x", 3, {"name": "no id"}, {"id": 5, "name": words(40)}]) == []
    assert task_field_findings([{"id": "T01", "name": 5, "success_criteria": None, "verification": ["x"]}]) == []


def test_task_field_limits_are_25_for_text_and_20_for_verification() -> None:
    task = {"id": "T01", "name": words(26) + ".", "success_criteria": words(26) + ".", "verification": words(21) + "."}
    assert [(finding.where, finding.message) for finding in task_field_findings([task])] == [
        ("T01 name", "sentence of 26 words (limit 25)"),
        ("T01 success_criteria", "sentence of 26 words (limit 25)"),
        ("T01 verification", "sentence of 21 words (limit 20)"),
    ]


def test_format_warning_names_the_label_and_caps_the_findings() -> None:
    assert format_warning("a.md", []) is None
    one = [Finding("L1", "length", "sentence of 30 words (limit 25)")]
    expected = "a.md (form only, never an error): L1 [length] sentence of 30 words (limit 25)"
    assert format_warning("a.md", one) == expected
    many = [Finding(f"L{number}", "verb", "'will': x") for number in range(1, MAX_FINDINGS + 4)]
    text = format_warning("a.md", many)
    assert text is not None
    assert text.count("[verb]") == MAX_FINDINGS
    assert text.endswith("; and 3 more (8 findings)")
    exact = format_warning("a.md", many[:MAX_FINDINGS])
    assert exact is not None
    assert "more" not in exact


def test_finding_prints_where_rule_and_message() -> None:
    assert str(Finding("L4", "word", "'x': write 'y'")) == "L4 [word] 'x': write 'y'"


def test_a_large_document_is_checked_in_linear_time() -> None:
    sentences = 40_000
    started = time.perf_counter()
    found = document_findings("A short sentence here. " * sentences)
    assert time.perf_counter() - started < 5
    assert found == [Finding("L1", "paragraph", f"paragraph of {sentences} sentences (limit 6)")]


def test_section_only_keeps_one_section_and_every_line_number() -> None:
    markdown = "# T\n\n## Keep Me\ntext one\n\n## Other\ntext two\n\n## keep me\ntext three\n"
    kept = section_only(markdown, "Keep me")
    assert kept.splitlines() == ["", "", "", "text one", "", "", "", "", "", "text three"]
    assert len(kept.splitlines()) == len(markdown.splitlines())


def test_section_only_of_a_missing_section_is_blank() -> None:
    assert section_only("# T\n\n## Other\ntext\n", "Absent").strip() == ""
    assert section_only("", "Absent") == ""
