"""Claim representation and conflict relation for parallel task execution.

Comparison keys arrive already resolved: this module never calls Path.resolve(),
never probes a volume for case sensitivity, never reads a symlink, and never
infers a resource from a command string. Correctness here is correctness of
comparisons over its inputs; filesystem identity normalization belongs to the
caller.
"""
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

_VALID_NAMESPACES = frozenset({"local", "store"})
_VALID_ACCESSES = frozenset({"read", "write"})


class ClaimValidationError(ValueError):
    """Raised when a claim contains an invalid field value."""


class Claim(NamedTuple):
    """Immutable record representing one access claim on a resource.

    namespace: "local" or "store". "external" is refused at validation.
    key: the already-resolved comparison key. For "local", an absolute POSIX
         path in canonical spelling. For "store", an opaque attempt id.
    access: "read" or "write".
    """

    namespace: str
    key: str
    access: str


class Conflict(NamedTuple):
    """Immutable record naming a conflicting claim pair and why they conflict.

    requested: the claim from the requesting grant.
    held: the claim from the holding grant.
    reason: "same_key", "ancestor", or "descendant" — the relationship of
            requested.key to held.key.
    """

    requested: Claim
    held: Claim
    reason: str


def _check_str_field(name: str, value: Any) -> None:
    """Raise ClaimValidationError if value is not a plain string or contains NUL."""
    if not isinstance(value, str):
        raise ClaimValidationError(f"{name} must be str, got {type(value).__name__!r}: {value!r}")
    if "\x00" in value:
        raise ClaimValidationError(f"{name} contains NUL: {value!r}")


def _validate_claim(claim: Claim) -> None:
    """Raise ClaimValidationError for any invalid field in claim."""
    _check_str_field("namespace", claim.namespace)
    _check_str_field("key", claim.key)
    _check_str_field("access", claim.access)

    if claim.namespace not in _VALID_NAMESPACES:
        raise ClaimValidationError(f"namespace must be 'local' or 'store', got {claim.namespace!r}")
    if claim.access not in _VALID_ACCESSES:
        raise ClaimValidationError(f"access must be 'read' or 'write', got {claim.access!r}")
    if not claim.key:
        raise ClaimValidationError("key must not be empty")

    if claim.namespace == "local":
        if not claim.key.startswith("/"):
            raise ClaimValidationError(f"local key must be absolute (start with '/'), got {claim.key!r}")
        if claim.key != "/" and claim.key.endswith("/"):
            raise ClaimValidationError(f"local key must not have a trailing slash, got {claim.key!r}")
        if "//" in claim.key:
            raise ClaimValidationError(f"local key must not have repeated separators, got {claim.key!r}")
        for part in claim.key.split("/"):
            if part in (".", ".."):
                raise ClaimValidationError(
                    f"local key must not contain '.' or '..' components, got {claim.key!r}"
                )


def _local_components(key: str) -> List[str]:
    """Return the non-empty path components of an absolute local key."""
    return [p for p in key.split("/") if p]


def _local_relationship(left_key: str, right_key: str) -> str:
    """Return 'same_key', 'ancestor', 'descendant', or '' for two local path keys."""
    left_parts = _local_components(left_key)
    right_parts = _local_components(right_key)
    if left_parts == right_parts:
        return "same_key"
    n_left = len(left_parts)
    n_right = len(right_parts)
    if n_left < n_right and right_parts[:n_left] == left_parts:
        return "ancestor"
    if n_right < n_left and left_parts[:n_right] == right_parts:
        return "descendant"
    return ""


def _conflict_reason(left: Claim, right: Claim) -> Optional[str]:
    """Return the conflict reason string, or None if the claims do not conflict."""
    if left.namespace != right.namespace:
        return None
    if left.access == "read" and right.access == "read":
        return None
    if left.namespace == "local":
        rel = _local_relationship(left.key, right.key)
        return rel if rel else None
    return "same_key" if left.key == right.key else None


def normalize_claims(claims: Iterable[Claim]) -> Tuple[Claim, ...]:
    """Return a validated, deduplicated, and sorted tuple of claims.

    Deduplication rules:
    - Identical (namespace, key, access) tuples collapse to one.
    - Where the same (namespace, key) appears as both "read" and "write",
      the "write" subsumes the "read".
    - Distinct parent and child (namespace, key) pairs are both preserved.

    Output order: sorted by (namespace, key, access), making this idempotent
    and reproducible across runs and interpreters.

    Raises ClaimValidationError for any malformed claim.
    """
    seen: Dict[Tuple[str, str], Set[str]] = {}
    for claim in claims:
        _validate_claim(claim)
        nk = (claim.namespace, claim.key)
        if nk not in seen:
            seen[nk] = set()
        seen[nk].add(claim.access)

    result: List[Claim] = []
    for (ns, key), accesses in seen.items():
        access = "write" if "write" in accesses else "read"
        result.append(Claim(namespace=ns, key=key, access=access))

    result.sort(key=lambda c: (c.namespace, c.key, c.access))
    return tuple(result)


def claims_conflict(left: Claim, right: Claim) -> bool:
    """Return True if left and right claims conflict.

    Two reads never conflict. Claims in different namespaces never conflict.
    For "local" namespace: equal, ancestor, or descendant keys conflict (by
    path component — /repo/a does not conflict with /repo/ab). For "store"
    namespace: only equal keys conflict; no ancestry is computed over opaque
    attempt ids.

    The relation is symmetric: claims_conflict(a, b) == claims_conflict(b, a).
    """
    return _conflict_reason(left, right) is not None


def find_conflicts(
    requested: Sequence[Claim],
    held: Sequence[Claim],
) -> Tuple[Conflict, ...]:
    """Return all conflicting pairs between two claim sequences from different grants.

    Precondition: all claims have already been validated (e.g. via normalize_claims).
    Grant boundaries are the caller's responsibility; this function does not enforce
    them. Passing one grant's claims as both arguments produces a meaningless result.

    The reason in each Conflict describes the relationship of requested.key to held.key:
    - "same_key": the keys are equal.
    - "ancestor": the requested key is an ancestor of the held key.
    - "descendant": the requested key is a descendant of the held key.
    """
    result: List[Conflict] = []
    for req in requested:
        for hld in held:
            reason = _conflict_reason(req, hld)
            if reason is not None:
                result.append(Conflict(requested=req, held=hld, reason=reason))
    return tuple(result)
