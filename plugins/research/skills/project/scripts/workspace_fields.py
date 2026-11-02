"""The key sets of every closed request object, in one leaf module.

Eleven readers used to keep their own literal copy of these sets, so the checker that refused a request,
the validator that judged saved state and `workflow <action> --schema` could disagree. Each reader now
imports the table here and formats `FieldErrors` in its own words.

This module imports nothing from the workspace, so `workspace_lib` can read it at module level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Checkpoint continuation. A request may also carry `import_legacy`; checkpoint metadata may not.
CONTINUATION_FIELDS = frozenset(
    {"next", "questions", "pointers", "partial", "ownership", "effects", "lessons", "do_not", "session", "stop_reason"}
)

FINDING_FIELDS = frozenset(
    {"id", "reviewer", "severity", "scope", "summary", "triage", "evidence", "resolution", "artifact"}
)
FINDING_REQUIRED = frozenset({"id", "reviewer", "severity", "scope", "summary", "triage"})
OPERATION_FIELDS = {
    "open": frozenset({"operation", "target", "worktree", "commit", "stages"}),
    "close": frozenset({"operation", "round", "reports", "findings", "confirmation"}),
    "waive": frozenset({"operation", "target", "worktree", "commit", "reason", "confirmation"}),
}
OPERATION_REQUIRED = {
    "open": frozenset({"target", "stages"}),
    "close": frozenset({"round", "reports", "findings"}),
    "waive": frozenset({"target", "reason", "confirmation"}),
}

_PROMOTION = frozenset({"topic", "body", "description", "kind", "scope", "expected_sha256"})
_PROMOTION_REQUIRED = frozenset({"topic", "body", "expected_sha256"})
_AUTHORIZATION = frozenset({"required", "status", "scope", "source", "authorized_at"})

# The optional delivery records of a worktree entry.
DELIVERY_FIELDS = frozenset({"pull_request", "pr_exemption", "version_line", "version_exemption", "merge"})

# A shape name maps to (allowed, required).
NESTED_INPUTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "confirmation": (frozenset({"source", "response"}), frozenset({"source", "response"})),
    "authorization": (_AUTHORIZATION, _AUTHORIZATION),
    "receipt": (
        frozenset({"kind", "value", "destination", "timestamp"}),
        frozenset({"kind", "value", "destination", "timestamp"}),
    ),
    "continuation": (CONTINUATION_FIELDS | {"import_legacy"}, frozenset()),
    "triage_item": (
        frozenset({"section", "disposition", "reason", "promotion"}),
        frozenset({"section", "disposition", "reason"}),
    ),
    "triage_promotion": (_PROMOTION, _PROMOTION_REQUIRED),
    "triage_promotion_journal": (_PROMOTION | {"entry_id"}, _PROMOTION_REQUIRED | {"entry_id"}),
}

# An action maps to its request keys that hold a nested object, in the order they are checked. A key
# ending in `[]` is a list of that shape.
ACTION_NESTED: dict[str, tuple[tuple[str, str], ...]] = {
    "worktree": (("confirmation", "confirmation"), ("continuation", "continuation")),
    "authorize": (("authorization", "authorization"), ("continuation", "continuation")),
    "receipt": (("receipt", "receipt"),),
    "checkpoint": (("continuation", "continuation"),),
    "round": (("continuation", "continuation"),),
    "confirm": (("continuation", "continuation"),),
    "correct": (("continuation", "continuation"),),
    "reconcile": (("continuation", "continuation"),),
    "finalize": (("continuation", "continuation"),),
    "maintenance": (("continuation", "continuation"),),
    "cancel": (("continuation", "continuation"),),
}

# Nested objects an action cannot do without: absent, they are refused as `key: expected a JSON object`.
NESTED_REQUIRED: dict[str, frozenset[str]] = {"worktree": frozenset({"confirmation"})}


@dataclass(frozen=True)
class FieldErrors:
    """What is wrong with one object's keys; each caller formats it in its own words."""

    not_object: str | None = None
    unknown: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.not_object or self.unknown or self.missing)


def field_errors(value: Any, allowed: Any, required: Any = frozenset()) -> FieldErrors:
    """Compare an object's keys with the allowed and required sets; sorted, and never raises."""
    if not isinstance(value, dict):
        return FieldErrors(not_object=type(value).__name__)
    return FieldErrors(None, sorted(value.keys() - allowed), sorted(set(required) - value.keys()))
