"""c3: id-list arguments of `task` accept a JSON array, a comma list or single ids."""

from __future__ import annotations

import manage_workspace
import pytest

BASE = ["task", "project", "finish", "T01", "--expected-revision", "1"]


def _ids(flag: str, *values: str) -> list[str]:
    args = manage_workspace._build_parser().parse_args([*BASE, *values])
    return getattr(args, flag)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["--evidence", "a", "b"], ["a", "b"]),
        (["--evidence", "a", "--evidence", "b"], ["a", "b"]),
        (["--evidence", "a,b"], ["a", "b"]),
        (["--evidence", '["a","b"]'], ["a", "b"]),
        (["--evidence", '["a", "b"]'], ["a", "b"]),
        (["--evidence", '[ "a" , "b" ]', "c"], ["a", "b", "c"]),
        (["--evidence", "[]"], []),
        (["--evidence", ' ["a", "b"]'], ["a", "b"]),
        (["--evidence", "a,b", "--evidence", '["c"]', "d"], ["a", "b", "c", "d"]),
        (["--evidence", "a,,b,"], ["a", "b"]),
        (["--evidence", ""], []),
    ],
)
def test_evidence_accepts_array_comma_and_plain_ids(values: list[str], expected: list[str]) -> None:
    assert _ids("evidence_records", *values) == expected


def test_observation_accepts_the_same_forms() -> None:
    assert _ids("observation_records", "--observation", "a,b", "--observation", '["c"]') == ["a", "b", "c"]


def test_no_list_flags_leave_empty_lists() -> None:
    assert _ids("evidence_records") == []
    assert _ids("observation_records") == []


def test_one_argument_with_spaces_names_the_zsh_cause(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _ids("evidence_records", "--evidence", "a b")
    assert raised.value.code == 2
    assert (
        "--evidence got one argument with spaces (a b); zsh does not word-split $var; "
        'pass a JSON array, a comma list, or "${ids[@]}"'
    ) in capsys.readouterr().err


def test_a_space_in_a_comma_list_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _ids("evidence_records", "--evidence", "a, b")
    assert "(a, b)" in capsys.readouterr().err


def test_observation_with_spaces_names_its_own_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _ids("observation_records", "--observation", "a b")
    assert "--observation got one argument with spaces (a b)" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ['["a",', '[{"a": 1}]', "[1, 2]", '["a", ""]', "[[]]"])
def test_malformed_arrays_are_refused(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _ids("evidence_records", "--evidence", bad)
    assert raised.value.code == 2
    assert f"--evidence expects a JSON array of non-empty strings, got {bad}" in capsys.readouterr().err
