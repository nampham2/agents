"""Tests for execution_claims: Claim representation and conflict relation.

Coverage must be 100% over execution_claims; every test establishes a named
property rather than merely describing a call.
"""
from __future__ import annotations

from typing import Any

import pytest
from execution_claims import (
    Claim,
    ClaimValidationError,
    Conflict,
    claims_conflict,
    find_conflicts,
    normalize_claims,
)

# --- helpers ---


def local_read(key: str) -> Claim:
    return Claim(namespace="local", key=key, access="read")


def local_write(key: str) -> Claim:
    return Claim(namespace="local", key=key, access="write")


def store_read(key: str) -> Claim:
    return Claim(namespace="store", key=key, access="read")


def store_write(key: str) -> Claim:
    return Claim(namespace="store", key=key, access="write")


# --- conflict rules ---


def test_read_read_never_conflicts() -> None:
    assert not claims_conflict(local_read("/repo/a"), local_read("/repo/a"))
    assert not claims_conflict(local_read("/repo/a"), local_read("/repo/a/b"))
    assert not claims_conflict(store_read("x"), store_read("x"))


def test_read_write_conflicts() -> None:
    assert claims_conflict(local_read("/repo/a"), local_write("/repo/a"))
    assert claims_conflict(local_write("/repo/a"), local_read("/repo/a"))


def test_write_write_conflicts() -> None:
    assert claims_conflict(local_write("/repo/a"), local_write("/repo/a"))
    assert claims_conflict(store_write("x"), store_write("x"))


# --- equal keys, both directions ---


def test_equal_local_keys_conflict_both_directions() -> None:
    a = local_write("/repo/a")
    b = local_write("/repo/a")
    assert claims_conflict(a, b)
    assert claims_conflict(b, a)


def test_equal_store_keys_conflict_both_directions() -> None:
    a = store_write("abc123")
    b = store_write("abc123")
    assert claims_conflict(a, b)
    assert claims_conflict(b, a)


# --- ancestor/descendant ---


def test_ancestor_conflicts_both_directions() -> None:
    parent = local_write("/repo/a")
    child = local_write("/repo/a/b")
    assert claims_conflict(parent, child)
    assert claims_conflict(child, parent)


def test_deeper_ancestor_conflicts_both_directions() -> None:
    grandparent = local_write("/repo/a")
    grandchild = local_write("/repo/a/b/c")
    assert claims_conflict(grandparent, grandchild)
    assert claims_conflict(grandchild, grandparent)


# --- prefix siblings do not conflict ---


def test_prefix_siblings_do_not_conflict() -> None:
    a = local_write("/repo/a")
    ab = local_write("/repo/ab")
    assert not claims_conflict(a, ab)
    assert not claims_conflict(ab, a)


# --- distinct roots do not conflict ---


def test_distinct_roots_do_not_conflict() -> None:
    assert not claims_conflict(local_write("/x/y"), local_write("/z/y"))


def test_unrelated_paths_different_depths_do_not_conflict() -> None:
    # Covers the branch where n_left < n_right but components don't share the prefix.
    assert not claims_conflict(local_write("/a"), local_write("/b/c"))
    # Covers the branch where n_right < n_left but components don't share the prefix.
    assert not claims_conflict(local_write("/b/c"), local_write("/a"))


# --- cross-namespace ---


def test_local_and_store_never_conflict() -> None:
    assert not claims_conflict(local_write("/repo/a"), store_write("/repo/a"))
    assert not claims_conflict(store_write("/repo/a"), local_write("/repo/a"))
    assert not claims_conflict(local_read("/repo/a"), store_read("/repo/a"))


# --- store exact-match only ---


def test_equal_store_ids_conflict() -> None:
    assert claims_conflict(store_write("attempt-42"), store_write("attempt-42"))


def test_different_store_ids_do_not_conflict() -> None:
    assert not claims_conflict(store_write("attempt-1"), store_write("attempt-2"))


def test_store_no_ancestry_applied() -> None:
    # Store keys are opaque: "a/b" is NOT treated as a child of "a".
    assert not claims_conflict(store_write("a"), store_write("a/b"))
    assert not claims_conflict(store_write("attempt/1"), store_write("attempt/1/sub"))


# --- root path is a valid local key ---


def test_local_root_path_is_valid() -> None:
    # "/" is an absolute path; the trailing-slash guard exempts it.
    result = normalize_claims([local_write("/")])
    assert result == (Claim(namespace="local", key="/", access="write"),)


# --- malformed input tests (one per boundary-2 case) ---


def test_unknown_namespace_raises() -> None:
    with pytest.raises(ClaimValidationError, match="namespace"):
        normalize_claims([Claim(namespace="external", key="/a", access="read")])


def test_another_unknown_namespace_raises() -> None:
    with pytest.raises(ClaimValidationError, match="namespace"):
        normalize_claims([Claim(namespace="remote", key="/a", access="read")])


def test_unknown_access_raises() -> None:
    with pytest.raises(ClaimValidationError, match="access"):
        normalize_claims([Claim(namespace="local", key="/a", access="execute")])


def test_empty_key_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="", access="read")])


def test_store_empty_key_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="store", key="", access="read")])


def test_local_key_not_absolute_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="relative/path", access="read")])


def test_local_key_dot_component_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="/repo/./a", access="read")])


def test_local_key_dotdot_component_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="/repo/../a", access="read")])


def test_local_key_trailing_slash_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="/repo/a/", access="read")])


def test_local_key_repeated_separator_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="/repo//a", access="read")])


def test_nul_in_namespace_raises() -> None:
    with pytest.raises(ClaimValidationError, match="namespace"):
        normalize_claims([Claim(namespace="loc\x00al", key="/a", access="read")])


def test_nul_in_key_raises() -> None:
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key="/a\x00b", access="read")])


def test_nul_in_access_raises() -> None:
    with pytest.raises(ClaimValidationError, match="access"):
        normalize_claims([Claim(namespace="local", key="/a", access="r\x00ead")])


def test_non_string_namespace_raises() -> None:
    bad_ns: Any = None
    with pytest.raises(ClaimValidationError, match="namespace"):
        normalize_claims([Claim(namespace=bad_ns, key="/a", access="read")])


def test_non_string_key_raises() -> None:
    bad_key: Any = 42
    with pytest.raises(ClaimValidationError, match="key"):
        normalize_claims([Claim(namespace="local", key=bad_key, access="read")])


def test_non_string_access_raises() -> None:
    bad_access: Any = True
    with pytest.raises(ClaimValidationError, match="access"):
        normalize_claims([Claim(namespace="local", key="/a", access=bad_access)])


# --- normalization ---


def test_duplicate_claims_collapse() -> None:
    result = normalize_claims([local_read("/a"), local_read("/a"), local_read("/a")])
    assert result == (local_read("/a"),)


def test_write_subsumes_read() -> None:
    result = normalize_claims([local_read("/a"), local_write("/a")])
    assert result == (local_write("/a"),)


def test_write_subsumes_read_reverse_order() -> None:
    result = normalize_claims([local_write("/a"), local_read("/a")])
    assert result == (local_write("/a"),)


def test_distinct_parent_and_child_both_survive_normalization() -> None:
    parent = local_write("/repo/a")
    child = local_write("/repo/a/b")
    result = normalize_claims([parent, child])
    assert parent in result
    assert child in result
    assert len(result) == 2


# --- normalization order and idempotency ---


def test_normalize_output_is_total_order() -> None:
    # Output order: namespace → key → access (alphabetical within each dimension).
    claims = [
        local_write("/z"),
        store_read("b"),
        local_read("/a"),
        store_write("a"),
        local_read("/b"),
    ]
    result = normalize_claims(claims)
    keys = [(c.namespace, c.key, c.access) for c in result]
    assert keys == sorted(keys)


def test_normalize_is_idempotent() -> None:
    claims = [local_write("/a"), local_read("/b"), store_write("x")]
    first = normalize_claims(claims)
    second = normalize_claims(list(first))
    assert first == second


# --- symmetry ---


def test_claims_conflict_is_symmetric_across_full_matrix() -> None:
    matrix = [
        local_read("/repo/a"),
        local_write("/repo/a"),
        local_read("/repo/a/b"),
        local_write("/repo/a/b"),
        local_read("/repo/ab"),
        local_write("/repo/ab"),
        store_read("x"),
        store_write("x"),
        store_read("y"),
        store_write("y"),
    ]
    for left in matrix:
        for right in matrix:
            assert claims_conflict(left, right) == claims_conflict(right, left), (
                f"asymmetric: claims_conflict({left!r}, {right!r})"
            )


# --- find_conflicts reasons ---


def test_find_conflicts_same_key_reason() -> None:
    result = find_conflicts([local_write("/repo/a")], [local_write("/repo/a")])
    assert len(result) == 1
    assert result[0].reason == "same_key"
    assert result[0].requested == local_write("/repo/a")
    assert result[0].held == local_write("/repo/a")


def test_find_conflicts_ancestor_reason() -> None:
    result = find_conflicts([local_write("/repo/a")], [local_write("/repo/a/b")])
    assert len(result) == 1
    assert result[0].reason == "ancestor"


def test_find_conflicts_descendant_reason() -> None:
    result = find_conflicts([local_write("/repo/a/b")], [local_write("/repo/a")])
    assert len(result) == 1
    assert result[0].reason == "descendant"


def test_find_conflicts_no_conflicts() -> None:
    result = find_conflicts([local_write("/x")], [local_write("/y")])
    assert result == ()


def test_find_conflicts_store_same_key_reason() -> None:
    result = find_conflicts([store_write("attempt-1")], [store_write("attempt-1")])
    assert len(result) == 1
    assert result[0].reason == "same_key"


def test_find_conflicts_reproducible_pairs() -> None:
    req = [local_write("/a"), local_write("/b")]
    hld = [local_write("/a"), local_write("/b")]
    result = find_conflicts(req, hld)
    assert result == (
        Conflict(local_write("/a"), local_write("/a"), "same_key"),
        Conflict(local_write("/b"), local_write("/b"), "same_key"),
    )


def test_find_conflicts_grant_violation_is_meaningless_not_error() -> None:
    # Passing one grant's claims as both arguments violates the precondition.
    # The module does not raise; the result is meaningless (self-conflicts reported).
    grant = [local_write("/repo/a")]
    result = find_conflicts(grant, grant)
    assert len(result) == 1


# --- case aliasing ---


def test_case_insensitive_aliasing_tested_as_equal_pre_normalized_keys() -> None:
    # This module does not detect filesystem aliases: /Repo/A and /repo/a are
    # different keys unless the caller has already normalized them to the same
    # canonical spelling. The conflict below arises because the caller passed
    # two pre-normalized keys that happen to be equal — not because this module
    # resolved case sensitivity.
    assert claims_conflict(local_write("/repo/a"), local_write("/repo/a"))
    # Different spellings are treated as different keys; no conflict.
    # This is not evidence that the module handles case normalization.
    assert not claims_conflict(local_write("/Repo/A"), local_write("/repo/a"))
