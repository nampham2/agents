"""A design-stage executable model of the parallel-execution protocol's decision functions.

`plugins/research/skills/project/references/parallel-execution.md` §20.2 promises this file. It is a
model, not an implementation: nothing here is imported by the shipped scripts, nothing is wired into
`research:project`, and there is no executor. The point is that the document's decision functions —
the label function of §6.1, the conflict relation of §8.2, the admission predicates of §8.1, the
dispatch gate of §7.3, and the crash-prefix recovery outcomes of §6.3 — are stated precisely enough
to run, and that the invariants the document claims for them are actually entailed by the rules as
written.

What this establishes is internal: the specified functions have the properties the specification
claims. What it cannot establish is that the specification is right. A defect the document and this
model share is invisible here, and nothing below touches a real filesystem, real concurrency, a real
host, or real timing. §20.2 says so in the document; it is repeated here so a green run is not
mistaken for a correctness result.

The canonical enums are read from `workspace_lib` rather than restated, because the point of the
authorization tests is agreement with the real validator's vocabulary.
"""

from __future__ import annotations

import itertools
import unittest
from typing import FrozenSet, Optional, Tuple

from workspace_lib import AUTHORIZATION_STATUSES, EFFECT_KINDS

from tests.conftest import REPO_ROOT  # noqa: F401  (imported for sys.path side effect)

# --------------------------------------------------------------------------------------------------
# §6.1 — the label function, and §5.2's finalization predicate
# --------------------------------------------------------------------------------------------------

# A record set is the set of record names present in an attempt's journal directory. Parameterised
# records collapse to their family here: `hold`, `uncertainty`, `heartbeat`, `disposition`. Rows 2 and
# 3 discriminate on a disposition's `resolution`, so a disposition is carried as a separate argument
# rather than as a bare name.

ABANDON_RESOLUTIONS = frozenset({"abandon", "retry"})
RECONCILE_RESOLUTIONS = frozenset({"accept_partial", "block"})
RESOLUTIONS = ABANDON_RESOLUTIONS | RECONCILE_RESOLUTIONS

# (row number, label, predicate, the records the row's condition positively requires). The fourth
# element is what makes the necessity test meaningful: perturbing a record the condition never
# mentions proves nothing, so only these are removed.
ROWS = (
    (1, "UNCERTAIN", lambda r, d: "uncertainty" in r and "stop-evidence" not in r,
     ("uncertainty",)),
    (2, "ABANDONED", lambda r, d: "disposition" in r and d in ABANDON_RESOLUTIONS,
     ("disposition",)),
    (3, "RECONCILED", lambda r, d: "disposition" in r and d in RECONCILE_RESOLUTIONS,
     ("disposition",)),
    (4, "INTEGRATED", lambda r, d: {"acceptance", "commit-observed"} <= r,
     ("acceptance", "commit-observed")),
    (5, "QUARANTINED", lambda r, d: "hold" in r and d is None, ("hold",)),
    (6, "STOPPED", lambda r, d: "stop-evidence" in r and d is None, ("stop-evidence",)),
    (7, "LAUNCH_FAILED", lambda r, d: "launch-failed" in r, ("launch-failed",)),
    (8, "VERIFIED", lambda r, d: "acceptance" in r and "commit-observed" not in r, ("acceptance",)),
    (9, "RESULT_READY", lambda r, d: {"result", "classification"} <= r and "acceptance" not in r,
     ("result", "classification")),
    (10, "PUBLISHED", lambda r, d: "result" in r and "classification" not in r, ("result",)),
    (11, "RUNNING", lambda r, d: {"launch", "heartbeat"} <= r and "result" not in r,
     ("launch", "heartbeat")),
    (12, "DISPATCHED", lambda r, d: "launch" in r and not {"heartbeat", "result"} & r, ("launch",)),
    (13, "DISPATCHING", lambda r, d: "start-permit" in r and "launch" not in r, ("start-permit",)),
    (14, "PREPARED", lambda r, d: r == {"prepared"}, ()),
)

LABELS = tuple(label for _, label, _, _ in ROWS)

# Rows 2, 3, 4 and 7 are §6.1's finalizable conditions. Terminality for *scheduling* is what these
# four are; terminality for claim-holding is `finalized`, which is a different question. Conflating
# the two is the defect review 03 named as R3-06.
FINALIZABLE_ROWS = (2, 3, 4, 7)
SCHEDULING_TERMINAL = ("ABANDONED", "RECONCILED", "INTEGRATED", "LAUNCH_FAILED")

# §6.2, T9-T14 and T25-T29: every state a hold may be written from. Bounded so that no hold is
# written into a finalizable condition, where rows 4 and 7 would shadow it.
HOLD_SOURCE_LABELS = (
    "PREPARED", "DISPATCHING", "DISPATCHED", "RUNNING", "PUBLISHED", "RESULT_READY", "VERIFIED",
    "UNCERTAIN", "STOPPED",
)

UNLABELLED = "<unreadable>"


def label(records: "frozenset[str]", disposition: "str | None" = None) -> str:
    """§6.1: the first matching row's label.

    `prepared` is a precondition of the whole table, and an unmatched set is reported rather than
    labelled — both are the document's rules, not conveniences of this model.
    """
    if "prepared" not in records:
        return UNLABELLED
    for _, name, predicate, _ in ROWS:
        if predicate(records, disposition):
            return name
    return UNLABELLED


def matching_rows(records: "frozenset[str]", disposition: "str | None" = None) -> "list[int]":
    if "prepared" not in records:
        return []
    return [n for n, _, predicate, _ in ROWS if predicate(records, disposition)]


def finalized(records: "frozenset[str]") -> bool:
    """§6.1's second predicate."""
    return "release" in records


def holds_claims(records: "frozenset[str]") -> bool:
    """§5.2: `prepared` present and `release` absent.

    Deliberately not a function of the label. That independence is the fix for R3-06, so it is the
    property most worth asserting.
    """
    return "prepared" in records and not finalized(records)


# One witness record set per row, in row order, each without `release` so the label and the
# finalization predicate are exercised independently.
WITNESSES = (
    (1, frozenset({"prepared", "start-permit", "launch", "uncertainty"}), None),
    (2, frozenset({"prepared", "start-permit", "launch", "uncertainty", "stop-evidence",
                   "disposition"}), "abandon"),
    (3, frozenset({"prepared", "start-permit", "launch", "result", "hold", "disposition"}), "block"),
    (4, frozenset({"prepared", "start-permit", "launch", "result", "classification",
                   "acceptance", "commit-observed"}), None),
    (5, frozenset({"prepared", "start-permit", "launch", "result", "hold"}), None),
    (6, frozenset({"prepared", "start-permit", "launch", "uncertainty", "stop-evidence"}), None),
    (7, frozenset({"prepared", "start-permit", "launch-failed"}), None),
    (8, frozenset({"prepared", "start-permit", "launch", "result", "classification",
                   "acceptance"}), None),
    (9, frozenset({"prepared", "start-permit", "launch", "result", "classification"}), None),
    (10, frozenset({"prepared", "start-permit", "launch", "result"}), None),
    (11, frozenset({"prepared", "start-permit", "launch", "heartbeat"}), None),
    (12, frozenset({"prepared", "start-permit", "launch"}), None),
    (13, frozenset({"prepared", "start-permit"}), None),
    (14, frozenset({"prepared"}), None),
)

ALL_RECORD_NAMES = (
    "prepared", "start-permit", "launch", "launch-failed", "heartbeat", "result",
    "classification", "hold", "uncertainty", "stop-evidence", "acceptance",
    "commit-observed", "disposition", "release",
)


# --------------------------------------------------------------------------------------------------
# §6.2 — the transition table as a reachability graph
# --------------------------------------------------------------------------------------------------

# §6.2 declares each row's source as a *label*, and §6.1 computes the label from the record set. The
# two can disagree: a row may declare a source that the label function never produces for any set the
# row could fire from, in which case the row is dead and whatever it writes is shadowed. That is the
# defect class this graph exists to catch, so the graph fires a row only when `label()` on the
# predecessor actually returns one of its declared sources — never when the table merely says so.
#
# Parameterised record families collapse to one name, as in §6.1: `hold/*`, `uncertainty/*`,
# `heartbeat/*`. A record already present is not written again, so such a row simply does not fire.

_PREPARED_TO_RUNNING = ("PREPARED", "DISPATCHING", "DISPATCHED", "RUNNING")
_PREPARED_TO_VERIFIED = (*_PREPARED_TO_RUNNING, "PUBLISHED", "RESULT_READY", "VERIFIED")

# (row id, declared source labels — `None` means the empty journal, record written, resolutions)
TABLE_62 = (
    ("T1", (None,), "prepared", None),
    ("T2", ("PREPARED",), "start-permit", None),
    ("T3", ("DISPATCHING",), "launch", None),
    ("T4", ("DISPATCHING",), "launch-failed", None),
    ("T5", ("DISPATCHING",), "uncertainty", None),
    ("T6", ("DISPATCHED",), "heartbeat", None),
    ("T7", ("DISPATCHED", "RUNNING"), "result", None),
    ("T8", ("PUBLISHED",), "classification", None),
    ("T9", ("PUBLISHED",), "hold", None),
    ("T10", ("PUBLISHED",), "hold", None),
    ("T11", ("PUBLISHED",), "hold", None),
    ("T12", ("PUBLISHED",), "hold", None),
    ("T13", ("RESULT_READY",), "acceptance", None),
    ("T14", ("RESULT_READY",), "hold", None),
    ("T15", ("VERIFIED",), "commit-observed", None),
    ("T16", ("INTEGRATED", "LAUNCH_FAILED"), "release", None),
    ("T17", ("DISPATCHED", "RUNNING"), "uncertainty", None),
    ("T18", ("DISPATCHED", "RUNNING"), "uncertainty", None),
    ("T19", ("UNCERTAIN",), "stop-evidence", None),
    ("T20", ("STOPPED", "QUARANTINED"), "disposition", ("block",)),
    ("T21", ("STOPPED", "QUARANTINED"), "disposition", ("accept_partial",)),
    ("T22", ("STOPPED", "QUARANTINED"), "disposition", ("retry",)),
    ("T23", ("STOPPED", "QUARANTINED"), "disposition", ("abandon",)),
    ("T24", ("RECONCILED", "ABANDONED"), "release", None),
    ("T25", ("PREPARED",), "hold", None),
    ("T26", _PREPARED_TO_RUNNING, "hold", None),
    ("T27", _PREPARED_TO_RUNNING, "hold", None),
    ("T28", (*_PREPARED_TO_VERIFIED, "UNCERTAIN", "STOPPED"), "hold", None),
    ("T29", (*_PREPARED_TO_VERIFIED, "UNCERTAIN", "STOPPED"), "hold", None),
)

State = Tuple[FrozenSet[str], Optional[str]]
EMPTY_STATE = (frozenset(), None)


def source_label(state: State) -> "str | None":
    records, disposition = state
    if not records:
        return None
    return label(records, disposition)


def successors(state: State) -> "list[tuple[str, State]]":
    src = source_label(state)
    records, disposition = state
    out = []
    for row, sources, record, resolutions in TABLE_62:
        if src not in sources or record in records:
            continue
        if resolutions is None:
            out.append((row, (records | {record}, disposition)))
        else:
            for resolution in resolutions:
                out.append((row, (records | {record}, resolution)))
    return out


def explore() -> "tuple[set[State], set[tuple[State, str, State]]]":
    """Every state §6.2 can actually produce, and every edge that produced one."""
    seen = {EMPTY_STATE}
    edges = set()
    frontier = [EMPTY_STATE]
    while frontier:
        state = frontier.pop()
        for row, nxt in successors(state):
            edges.add((state, row, nxt))
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen, edges


REACHABLE, EDGES = explore()

# --------------------------------------------------------------------------------------------------
# §8.2 — claims and the conflict relation
# --------------------------------------------------------------------------------------------------


class Claim:
    """`{namespace, key, access}` with the two namespaces of §8.2."""

    __slots__ = ("access", "key", "namespace")

    def __init__(self, namespace: str, key: str, access: str) -> None:
        if namespace not in {"path", "external"}:
            raise ValueError(f"unknown namespace {namespace!r}")
        if access not in {"read", "write"}:
            raise ValueError(f"unknown access {access!r}")
        if namespace == "path" and not key.startswith("/"):
            # §8.2: "A bare name is never a claim." The derivation error names the offending value.
            raise ValueError(f"path claim is not an absolute resolved path: {key!r}")
        self.namespace = namespace
        self.key = key
        self.access = access

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Claim({self.namespace}:{self.key} {self.access})"


def _segments(namespace: str, key: str) -> "list[str]":
    separator = "/"
    return [part for part in key.split(separator) if part] if namespace == "path" else \
        [part for part in key.split("/") if part]


def is_proper_ancestor(namespace: str, a: str, b: str) -> bool:
    """Component-wise ancestry, so `/repo-a` is not an ancestor of `/repo-b`."""
    first, second = _segments(namespace, a), _segments(namespace, b)
    return len(first) < len(second) and second[: len(first)] == first


def conflict(a: Claim, b: Claim) -> bool:
    """§8.2's relation, written once and tested in both directions by construction."""
    if a.namespace != b.namespace:
        return False
    if a.access == "read" and b.access == "read":
        return False
    return (
        a.key == b.key
        or is_proper_ancestor(a.namespace, a.key, b.key)
        or is_proper_ancestor(a.namespace, b.key, a.key)
    )


# --------------------------------------------------------------------------------------------------
# §8.1 — the two admission predicates
# --------------------------------------------------------------------------------------------------


def delegable(effect_kind: str, required: bool, status: str) -> bool:
    """§8.1 condition 3."""
    return effect_kind in {"none", "local_write"} and not required and status == "not_required"


def in_force(required: bool, status: str, scope: "str | None", frozen_scope: "str | None") -> bool:
    """§8.1 condition 4."""
    if not required:
        return status == "not_required"
    return status == "explicit" and scope is not None and scope == frozen_scope


# --------------------------------------------------------------------------------------------------
# §7.3 — the dispatch gate
# --------------------------------------------------------------------------------------------------


def dispatch_blocked(
    attempts: "list[tuple[frozenset[str], str | None]]",
    schema_version: int = 4,
    ownership_current: bool = True,
) -> bool:
    """§7.3: a query over the journal, never a stored flag.

    The `"hold" not in records` conjunct in the first clause is the fix this model forced. Rows 2-5
    of §6.1's classification table quarantine a publication *instead of* classifying it, so
    `result ∧ ¬classification` stays true for a quarantined publication forever and clause 1 without
    the conjunct never clears.
    """
    for records, disposition in attempts:
        if "result" in records and "classification" not in records and "hold" not in records:
            return True
        if "hold" in records and disposition is None:
            return True
        if "uncertainty" in records and "stop-evidence" not in records:
            return True
    return schema_version != 4 or not ownership_current


# --------------------------------------------------------------------------------------------------
# §6.3 — crash prefixes and their deterministic recovery outcomes
# --------------------------------------------------------------------------------------------------

# Each multi-file transition is an ordered tuple of durable writes. A crash may land after any
# prefix, so the model enumerates every prefix and asks for exactly one outcome.
TRANSITIONS = {
    "dispatch": ("canonical-running", "start-permit", "launch"),
    "accept-and-commit": ("acceptance", "canonical-accept", "commit-observed"),
    "finalize": ("canonical-final", "release"),
    "launch-failure": ("launch-failed", "canonical-todo", "release"),
    "reconcile": ("disposition", "canonical-disposition", "release"),
    "collection": ("tombstone", "files-removed"),
}

OUTCOMES = {
    ("dispatch", 0): "re-prepare or release; no host call can have happened",
    ("dispatch", 1): "write the permit and proceed, or commit RUNNING -> TODO and release",
    ("dispatch", 2): "discover(launch_key); failing that write uncertainty/dispatch_interrupted",
    ("dispatch", 3): "complete: the attempt is DISPATCHED",
    ("accept-and-commit", 0): "no acceptance: classification stands, re-decide adequacy",
    ("accept-and-commit", 1): "re-read canonical state; retry the same commit with the same bytes",
    ("accept-and-commit", 2): "write commit-observed",
    ("accept-and-commit", 3): "complete: the attempt is INTEGRATED*",
    ("finalize", 0): "re-run the required canonical commit idempotently, then release",
    ("finalize", 1): "write release",
    ("finalize", 2): "complete: finalization done",
    ("launch-failure", 0): "no launch-failed: the dispatch prefix rules apply",
    ("launch-failure", 1): "commit RUNNING -> TODO, then release",
    ("launch-failure", 2): "write release",
    ("launch-failure", 3): "complete: the attempt is LAUNCH_FAILED",
    ("reconcile", 0): "no disposition: the hold or stop-evidence stands",
    ("reconcile", 1): "apply the disposition's canonical status, then release",
    ("reconcile", 2): "write release",
    ("reconcile", 3): "complete: the attempt is RECONCILED or ABANDONED",
    ("collection", 0): "nothing was promised; collection has not started",
    ("collection", 1): "re-remove the named paths; a missing named path is collected, not lost",
    ("collection", 2): "complete: collection done",
}


def recovery_outcome(transition: str, prefix_length: int) -> str:
    """The single outcome §6.3 assigns to one crash prefix."""
    return OUTCOMES[(transition, prefix_length)]


def apply_outcome(transition: str, prefix_length: int) -> int:
    """Applying a recovery outcome completes the transition. Idempotence is modelled as a fixpoint.

    Recovery drives a prefix to the full write sequence. Applying it again from the completed state
    is a no-op, which is what "tolerates having already happened" means in §6.3.
    """
    del prefix_length
    return len(TRANSITIONS[transition])


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------


class LabelFunctionTest(unittest.TestCase):
    """§6.1: the label is a total, deterministic, first-match function of the record set, and it is
    a different question from whether the attempt still holds its claims."""

    def test_the_table_is_a_bijection_onto_the_labels(self) -> None:
        """Revision 3 had fifteen rows for fourteen labels because `release` was folded into three
        row conditions. One row per label is what makes `label()` and `finalized()` separable."""
        self.assertEqual(14, len(ROWS))
        self.assertEqual(14, len(set(LABELS)))
        self.assertEqual([n for n, _, _, _ in ROWS], list(range(1, 15)))

    def test_every_row_has_a_witness_that_reaches_it(self) -> None:
        self.assertEqual(len(ROWS), len(WITNESSES))
        for row, records, disposition in WITNESSES:
            with self.subTest(row=row):
                matched = matching_rows(records, disposition)
                self.assertTrue(matched, f"row {row}'s witness matches nothing")
                self.assertEqual(
                    row, matched[0],
                    f"row {row}'s witness is captured earlier by row {matched[0]}",
                )

    def test_every_label_is_reachable(self) -> None:
        reached = {label(records, disposition) for _, records, disposition in WITNESSES}
        self.assertEqual(set(LABELS), reached)

    def test_each_row_condition_is_necessary(self) -> None:
        """Remove a record the row's condition positively requires, and the row must stop matching.

        Only the records the condition names are perturbed. Removing an unrelated record proves
        nothing about the row, and asserting on it is how a necessity test becomes noise.
        """
        for row, records, disposition in WITNESSES:
            predicate = ROWS[row - 1][2]
            required = ROWS[row - 1][3]
            self.assertTrue(predicate(records, disposition), f"row {row}'s witness does not match")
            for name in required:
                with self.subTest(row=row, without=name):
                    self.assertIn(name, records, f"row {row} requires {name} but omits it")
                    self.assertFalse(
                        predicate(frozenset(records - {name}), disposition),
                        f"row {row} still matches without {name}, so the conjunct is decoration",
                    )

    def test_row_14_is_the_empty_case_and_needs_no_required_record(self) -> None:
        """`PREPARED` is `prepared` alone. Its condition names nothing else, which is why it is the
        one row with an empty required set rather than an omission."""
        self.assertEqual((), ROWS[13][3])
        self.assertEqual("PREPARED", label(frozenset({"prepared"})))

    def test_disposition_resolution_discriminates(self) -> None:
        records = frozenset({"prepared", "start-permit", "launch", "result", "hold", "disposition"})
        self.assertEqual("ABANDONED", label(records, "abandon"))
        self.assertEqual("ABANDONED", label(records, "retry"), "T22 writes retry; row 2 must take it")
        self.assertEqual("RECONCILED", label(records, "block"))
        self.assertEqual("RECONCILED", label(records, "accept_partial"))
        self.assertEqual(
            ABANDON_RESOLUTIONS | RECONCILE_RESOLUTIONS, RESOLUTIONS,
            "the two rows must partition the resolutions, with none left unlabelled",
        )
        self.assertEqual(frozenset(), ABANDON_RESOLUTIONS & RECONCILE_RESOLUTIONS)

    def test_no_record_set_needs_two_labels(self) -> None:
        """First-match resolution means one label. What is worth checking is that wherever two rows
        match, the answer is the earlier row — the document's order, not the model's iteration."""
        names = [n for n in ALL_RECORD_NAMES if n != "prepared"]
        overlaps = 0
        for size in range(0, 4):
            for combination in itertools.combinations(names, size):
                records = frozenset(("prepared", *combination))
                for disposition in (None, *sorted(RESOLUTIONS)):
                    if ("disposition" in records) != (disposition is not None):
                        continue
                    matched = matching_rows(records, disposition)
                    if len(matched) > 1:
                        overlaps += 1
                        with self.subTest(records=sorted(records), disposition=disposition):
                            self.assertEqual(matched[0], min(matched))
                            self.assertEqual(ROWS[matched[0] - 1][1], label(records, disposition))
        self.assertGreater(overlaps, 0, "no set matched two rows, so first-match was never tested")

    def test_uncertainty_outranks_every_other_row(self) -> None:
        """Consequence 1 of §6.1. A possible live writer must never be hidden behind a hold, a
        result, or a disposition, because every other row's consequence assumes the writer stopped."""
        names = [n for n in ALL_RECORD_NAMES if n not in {"prepared", "stop-evidence"}]
        for size in range(0, 3):
            for combination in itertools.combinations(names, size):
                records = frozenset(("prepared", "uncertainty", *combination))
                disposition = "abandon" if "disposition" in records else None
                with self.subTest(records=sorted(records)):
                    self.assertEqual("UNCERTAIN", label(records, disposition))

    def test_hold_outranks_progress_but_not_uncertainty(self) -> None:
        """Consequence 2, at the scope the document states: rows 8-13, the states whose canonical
        status is still `RUNNING`. Row 4 is *above* row 5, which is why §6.2 bounds the hold sources
        instead of relying on this ordering to cover them."""
        published = frozenset({"prepared", "start-permit", "launch", "result"})
        self.assertEqual("PUBLISHED", label(published))
        self.assertEqual("QUARANTINED", label(published | {"hold"}))
        verified = published | {"classification", "acceptance"}
        self.assertEqual("VERIFIED", label(verified))
        self.assertEqual("QUARANTINED", label(verified | {"hold"}))
        self.assertEqual("UNCERTAIN", label(verified | {"hold", "uncertainty"}))

    def test_unmatched_sets_are_reported_not_labelled(self) -> None:
        """`prepared` is a precondition of the whole table (§6.1), so a set without it is
        unreadable rather than mislabelled. This is the check that found the gap: before the
        precondition was stated, `{launch}` silently labelled `DISPATCHED`."""
        for records in (frozenset(), frozenset({"launch"}), frozenset({"result"}),
                        frozenset({"start-permit", "launch", "heartbeat"})):
            with self.subTest(records=sorted(records)):
                self.assertEqual(UNLABELLED, label(records))
                self.assertEqual([], matching_rows(records))

    def test_finalization_is_orthogonal_to_the_label(self) -> None:
        """R3-06, and the defect this model found in revision 3.

        Adding `release` must not move a finalizable attempt to a different row. Revision 3 required
        `release` inside rows 1-3, so an attempt that had written its disposition and crashed before
        its release matched *no row at all* — a crash window with no label and therefore no recovery
        rule. Separating the two predicates is what closes it.
        """
        for row, records, disposition in WITNESSES:
            if row not in FINALIZABLE_ROWS:
                continue
            with self.subTest(row=row, label=label(records, disposition)):
                self.assertFalse(finalized(records))
                self.assertTrue(holds_claims(records))
                released = records | {"release"}
                self.assertEqual(
                    label(records, disposition), label(released, disposition),
                    "release changed the label, so the two predicates are entangled again",
                )
                self.assertTrue(finalized(released))
                self.assertFalse(holds_claims(released))

    def test_every_witness_holds_its_claims_before_release(self) -> None:
        for row, records, disposition in WITNESSES:
            with self.subTest(row=row, label=label(records, disposition)):
                self.assertFalse(finalized(records))
                self.assertTrue(holds_claims(records))

    def test_release_is_unreachable_from_prepared(self) -> None:
        """Row 14's `prepared` *only* excludes `release`, and §6.2 backs that: T16 and T24 are the
        only rows writing `release` and neither has `PREPARED` as a source. So `{prepared, release}`
        is unreachable, and the table must report it rather than invent a label for it."""
        self.assertEqual(UNLABELLED, label(frozenset({"prepared", "release"})))
        self.assertNotIn("PREPARED", {ROWS[n - 1][1] for n in FINALIZABLE_ROWS})

    def test_a_hold_is_never_shadowed_from_any_permitted_source(self) -> None:
        """§6.2's bounded hold sources, checked as a property of §6.1's order.

        Revision 3 let T28 and T29 write a hold from "any unresolved" state, which includes
        `INTEGRATED`* and `LAUNCH_FAILED`*. Rows 4 and 7 precede row 5, so the hold would leave the
        label untouched and change no decision. Every permitted source must therefore either become
        `QUARANTINED` or stay `UNCERTAIN` — the one state that outranks a hold on purpose.
        """
        for row, records, disposition in WITNESSES:
            name = label(records, disposition)
            if name not in HOLD_SOURCE_LABELS:
                continue
            with self.subTest(row=row, label=name):
                held = label(records | {"hold"}, disposition)
                expected = "UNCERTAIN" if name == "UNCERTAIN" else "QUARANTINED"
                self.assertEqual(expected, held, f"a hold written from {name} is shadowed as {held}")

    def test_no_hold_source_is_finalizable(self) -> None:
        """The invariant is disjointness, not equality. `QUARANTINED` is a non-finalizable label that
        is deliberately *not* a hold source: a quarantined attempt is owed a disposition, and §6.2
        routes supersession of one through T22 rather than through a second hold."""
        finalizable = {ROWS[n - 1][1] for n in FINALIZABLE_ROWS}
        self.assertEqual(frozenset(), finalizable & set(HOLD_SOURCE_LABELS))
        self.assertLess(set(HOLD_SOURCE_LABELS), set(LABELS))
        self.assertNotIn("QUARANTINED", HOLD_SOURCE_LABELS)

    def test_a_writer_that_may_be_live_never_appears_released(self) -> None:
        """Priority 6 of the revision brief: quarantined or uncertain writers keep ownership until
        execution is demonstrably stopped."""
        for row, records, disposition in WITNESSES:
            name = label(records, disposition)
            if name in {"UNCERTAIN", "QUARANTINED", "STOPPED"}:
                with self.subTest(row=row, label=name):
                    self.assertTrue(holds_claims(records))
                    self.assertNotIn(name, SCHEDULING_TERMINAL)

    def test_exactly_four_rows_are_finalizable(self) -> None:
        finalizable = {ROWS[n - 1][1] for n in FINALIZABLE_ROWS}
        self.assertEqual(set(SCHEDULING_TERMINAL), finalizable)
        self.assertEqual(4, len(FINALIZABLE_ROWS))


class ReachabilityTest(unittest.TestCase):
    """§6.2 read as a graph, so the claims that depend on what the table can *produce* are derived
    rather than assumed. Each row fires only when §6.1's label function actually returns one of its
    declared sources, which is what makes a shadowed row visible as a dead row."""

    def test_the_graph_is_not_trivial(self) -> None:
        self.assertGreater(len(REACHABLE), 100)
        self.assertGreater(len(EDGES), len(REACHABLE))

    def test_every_reachable_state_has_a_label(self) -> None:
        """The strongest property this model checks, and the one revision 3 failed: §6.1 must be
        total over everything §6.2 can produce, including every crash-window intermediate."""
        for records, disposition in sorted(REACHABLE, key=lambda st: (len(st[0]), sorted(st[0]))):
            if not records:
                continue
            with self.subTest(records=sorted(records), disposition=disposition):
                self.assertNotEqual(UNLABELLED, label(records, disposition))

    def test_no_row_is_dead(self) -> None:
        """A row whose declared source §6.1 never produces would write a record that changes nothing.
        Revision 3's T28 and T29 declared "any unresolved", which included `INTEGRATED`* — the row
        fired but its hold was shadowed. Bounding the sources is what makes every row live."""
        declared = {row for row, _, _, _ in TABLE_62}
        fired = {row for _, row, _ in EDGES}
        self.assertEqual(declared, fired, f"rows that never fire: {sorted(declared - fired)}")

    def test_a_disposition_is_written_only_from_quarantined_or_stopped(self) -> None:
        """§6.1's consequence 4, checked against the labels the graph actually computes rather than
        against §6.2's own prose."""
        sources = {
            source_label(before)
            for before, _, after in EDGES
            if "disposition" in after[0] and "disposition" not in before[0]
        }
        self.assertEqual({"QUARANTINED", "STOPPED"}, sources)

    def test_a_hold_always_lands_as_quarantine_or_stays_uncertain(self) -> None:
        for before, row, after in sorted(EDGES, key=lambda e: e[1]):
            if "hold" in before[0] or "hold" not in after[0]:
                continue
            src = source_label(before)
            with self.subTest(row=row, source=src):
                self.assertIn(src, HOLD_SOURCE_LABELS)
                expected = "UNCERTAIN" if src == "UNCERTAIN" else "QUARANTINED"
                self.assertEqual(expected, label(*after))

    def test_release_appears_only_under_a_finalizable_label(self) -> None:
        finalizable = {ROWS[n - 1][1] for n in FINALIZABLE_ROWS}
        for records, disposition in REACHABLE:
            if "release" in records:
                with self.subTest(records=sorted(records)):
                    self.assertIn(label(records, disposition), finalizable)

    def test_a_possibly_live_writer_never_carries_a_release(self) -> None:
        """Priority 6 of the revision brief, as a reachability property: no path through §6.2 reaches
        `UNCERTAIN`, `QUARANTINED` or `STOPPED` with the grant already dropped."""
        for records, disposition in REACHABLE:
            if not records:
                continue
            if label(records, disposition) in {"UNCERTAIN", "QUARANTINED", "STOPPED"}:
                with self.subTest(records=sorted(records)):
                    self.assertNotIn("release", records)
                    self.assertTrue(holds_claims(records))

    def test_prepared_with_a_release_is_unreachable(self) -> None:
        self.assertNotIn((frozenset({"prepared", "release"}), None), REACHABLE)

    def test_the_states_with_no_successor_are_exactly_the_finalized_ones(self) -> None:
        outgoing = {before for before, _, _ in EDGES}
        terminal = {label(*st) for st in REACHABLE if st not in outgoing}
        self.assertEqual(set(SCHEDULING_TERMINAL), terminal)
        for st in REACHABLE:
            if st not in outgoing:
                with self.subTest(records=sorted(st[0])):
                    self.assertTrue(finalized(st[0]))
                    self.assertFalse(holds_claims(st[0]))

    def test_the_co_occurrences_the_label_table_never_has_to_rank(self) -> None:
        """Derived, not assumed. These pairs are why rows 2-4 may sit above row 5 without shadowing
        anything: no path produces them, so the ordering between them is never consulted."""
        for pair in (("commit-observed", "hold"), ("commit-observed", "stop-evidence"),
                     ("commit-observed", "uncertainty"), ("launch", "launch-failed")):
            with self.subTest(pair=pair):
                self.assertEqual(
                    [], [sorted(r) for r, _ in REACHABLE if set(pair) <= r],
                    f"{pair} is reachable, so §6.1's ordering between its rows is load-bearing",
                )

    def test_no_reachable_state_blocks_dispatch_forever(self) -> None:
        """Liveness, and the reason §7.3's first clause carries `no hold`.

        From every reachable state that blocks dispatch there must be a path to one that does not.
        Without the conjunct, a quarantined publication satisfies `result ∧ ¬classification` for the
        rest of the journal's life and no continuation clears it — the project deadlocks. This model
        found that by exhaustion, not by inspection.
        """
        successor_map: "dict[State, set[State]]" = {}
        for before, _, after in EDGES:
            successor_map.setdefault(before, set()).add(after)

        def escapes(start: State) -> bool:
            seen = {start}
            stack = [start]
            while stack:
                state = stack.pop()
                if not dispatch_blocked([state]):
                    return True
                for nxt in successor_map.get(state, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            return False

        for state in sorted(REACHABLE, key=lambda st: (len(st[0]), sorted(st[0]))):
            if not state[0] or not dispatch_blocked([state]):
                continue
            with self.subTest(records=sorted(state[0]), disposition=state[1]):
                self.assertTrue(escapes(state), "no continuation of this state ever admits dispatch")


class ConflictRelationTest(unittest.TestCase):
    """§8.2: one relation, symmetric, ancestor-sensitive in both directions."""

    SAMPLE = (
        ("path", "/repo", "write"),
        ("path", "/repo", "read"),
        ("path", "/repo/plugins", "write"),
        ("path", "/repo/plugins", "read"),
        ("path", "/repo/plugins/b.py", "write"),
        ("path", "/repo-b", "write"),
        ("path", "/repo-b/plugins", "write"),
        ("external", "gitlab/project/branch", "write"),
        ("external", "gitlab/project", "write"),
        ("external", "gitlab/other", "write"),
    )

    def claims(self) -> "list[Claim]":
        return [Claim(*spec) for spec in self.SAMPLE]

    def test_bare_names_are_derivation_errors(self) -> None:
        for key in ("PROTECTED", "the repository", "repo/plugins"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    Claim("path", key, "write")

    def test_relation_is_symmetric(self) -> None:
        for a, b in itertools.product(self.claims(), repeat=2):
            with self.subTest(a=repr(a), b=repr(b)):
                self.assertEqual(conflict(a, b), conflict(b, a))

    def test_write_conflicts_with_itself(self) -> None:
        for claim in self.claims():
            if claim.access == "write":
                with self.subTest(claim=repr(claim)):
                    self.assertTrue(conflict(claim, claim))

    def test_two_reads_never_conflict(self) -> None:
        for a, b in itertools.product([c for c in self.claims() if c.access == "read"], repeat=2):
            with self.subTest(a=repr(a), b=repr(b)):
                self.assertFalse(conflict(a, b))

    def test_repository_write_conflicts_with_a_file_inside_it_both_ways(self) -> None:
        """R3-03's failure trace, as the assertion that the fix is real."""
        repo = Claim("path", "/repo", "write")
        inner = Claim("path", "/repo/plugins/b.py", "write")
        self.assertTrue(conflict(repo, inner))
        self.assertTrue(conflict(inner, repo))

    def test_global_read_excludes_writers_and_admits_readers(self) -> None:
        global_read = Claim("path", "/repo", "read")
        self.assertTrue(conflict(global_read, Claim("path", "/repo/b.py", "write")))
        self.assertFalse(conflict(global_read, Claim("path", "/repo/b.py", "read")))

    def test_string_prefix_siblings_do_not_conflict(self) -> None:
        self.assertFalse(conflict(
            Claim("path", "/repo", "write"), Claim("path", "/repo-b", "write")))
        self.assertFalse(conflict(
            Claim("path", "/repo/plugins", "write"), Claim("path", "/repo-b/plugins", "write")))

    def test_namespaces_do_not_cross(self) -> None:
        self.assertFalse(conflict(
            Claim("path", "/repo", "write"), Claim("external", "repo", "write")))

    def test_external_ancestry_is_on_segments(self) -> None:
        self.assertTrue(conflict(
            Claim("external", "gitlab/project", "write"),
            Claim("external", "gitlab/project/branch", "write")))
        self.assertFalse(conflict(
            Claim("external", "gitlab/project", "write"),
            Claim("external", "gitlab/other", "write")))

    def test_verification_under_its_own_grant_requests_nothing(self) -> None:
        """§8.2: a coordinator re-running a check for its own attempt adds no claim.

        Modelled as the property that matters: the attempt's own grant set is unchanged, so the
        self-conflict of revision 3 cannot arise by construction.
        """
        granted = [Claim("path", "/repo/out.txt", "write")]
        rerun_requests: "list[Claim]" = []
        combined = granted + rerun_requests
        self.assertEqual(len(granted), len(combined))
        for a, b in itertools.combinations(combined, 2):  # pragma: no cover - empty by construction
            self.assertFalse(conflict(a, b))


class AdmissionPredicateTest(unittest.TestCase):
    """§8.1: `delegable` and `in_force` over the real canonical enums."""

    def test_effect_kinds_come_from_the_validator(self) -> None:
        self.assertEqual({"none", "local_write", "destructive", "external"}, set(EFFECT_KINDS))

    def test_authorization_statuses_come_from_the_validator(self) -> None:
        self.assertEqual(
            {"not_required", "pending", "explicit", "denied", "deferred"},
            set(AUTHORIZATION_STATUSES),
        )

    def test_revoked_is_not_a_canonical_status(self) -> None:
        """R3-05: revision 3's T21 named a status the validator does not have."""
        self.assertNotIn("revoked", AUTHORIZATION_STATUSES)

    def test_delegable_is_exactly_local_unauthorized_work(self) -> None:
        for kind, required, status in itertools.product(
            sorted(EFFECT_KINDS), (True, False), sorted(AUTHORIZATION_STATUSES)
        ):
            expected = kind in {"none", "local_write"} and not required and status == "not_required"
            with self.subTest(kind=kind, required=required, status=status):
                self.assertEqual(expected, delegable(kind, required, status))

    def test_destructive_and_external_are_never_delegable(self) -> None:
        for kind in ("destructive", "external"):
            with self.subTest(kind=kind):
                self.assertFalse(delegable(kind, False, "not_required"))

    def test_pending_denied_and_deferred_are_not_in_force(self) -> None:
        for status in ("pending", "denied", "deferred"):
            with self.subTest(status=status):
                self.assertFalse(in_force(True, status, "scope", "scope"))
                self.assertFalse(in_force(False, status, None, None))

    def test_explicit_is_in_force_only_at_the_frozen_scope(self) -> None:
        self.assertTrue(in_force(True, "explicit", "push branch X", "push branch X"))
        self.assertFalse(in_force(True, "explicit", "push branch Y", "push branch X"))
        self.assertFalse(in_force(True, "explicit", None, "push branch X"))

    def test_not_required_is_in_force_only_with_the_matching_status(self) -> None:
        self.assertTrue(in_force(False, "not_required", None, None))
        for status in sorted(AUTHORIZATION_STATUSES - {"not_required"}):
            with self.subTest(status=status):
                self.assertFalse(in_force(False, status, None, None))

    def test_withdrawal_is_representable_as_one_status_change(self) -> None:
        """§14.1: withdrawal sets `denied`, which is in the enum and is not in force."""
        self.assertIn("denied", AUTHORIZATION_STATUSES)
        self.assertTrue(in_force(True, "explicit", "s", "s"))
        self.assertFalse(in_force(True, "denied", "s", "s"))


class DispatchGateTest(unittest.TestCase):
    """§7.3: the gate is a query, so the write that creates the failure creates the block."""

    def test_no_attempts_and_a_current_owner_admits_dispatch(self) -> None:
        self.assertFalse(dispatch_blocked([]))

    def test_published_failure_with_no_flag_anywhere_blocks(self) -> None:
        """R3-08 and §13.1: recovery needs no pause flag to have survived the crash."""
        attempts = [(frozenset({"prepared", "start-permit", "launch", "result"}), None)]
        self.assertFalse(any("flag" in records for records, _ in attempts))
        self.assertTrue(dispatch_blocked(attempts))

    def test_classifying_the_publication_clears_that_reason(self) -> None:
        classified = [(frozenset({"prepared", "start-permit", "launch", "result",
                                  "classification"}), None)]
        self.assertFalse(dispatch_blocked(classified))

    def test_unresolved_hold_blocks_until_a_disposition_exists(self) -> None:
        """And it must actually clear. A quarantined publication satisfies clause 1's first two
        conjuncts permanently, so without clause 1's `no hold` the disposition would change
        nothing and the project could never dispatch again."""
        held = frozenset({"prepared", "start-permit", "launch", "result", "hold"})
        self.assertTrue(dispatch_blocked([(held, None)]))
        resolved = held | {"disposition"}
        self.assertFalse(dispatch_blocked([(resolved, "block")]))
        self.assertFalse(dispatch_blocked([(resolved | {"release"}, "block")]))

    def test_uncertainty_blocks_until_stop_evidence_exists(self) -> None:
        """Clause 3 asks only whether a writer may still be running. A `STOPPED` attempt is
        demonstrably not running, so it stops blocking *dispatch* — it keeps its claims instead
        (§6.1's second predicate), and the claims are what keep a conflicting task out."""
        uncertain = frozenset({"prepared", "start-permit", "launch", "uncertainty"})
        self.assertTrue(dispatch_blocked([(uncertain, None)]))
        stopped = uncertain | {"stop-evidence"}
        self.assertFalse(dispatch_blocked([(stopped, None)]))
        self.assertTrue(holds_claims(stopped))
        self.assertEqual("STOPPED", label(stopped))
        released = stopped | {"disposition", "release"}
        self.assertFalse(dispatch_blocked([(released, "abandon")]))
        self.assertFalse(holds_claims(released))

    def test_wrong_schema_version_and_lost_ownership_block(self) -> None:
        self.assertTrue(dispatch_blocked([], schema_version=3))
        self.assertTrue(dispatch_blocked([], ownership_current=False))

    def test_the_blocking_attempt_is_the_one_that_wrote_the_record(self) -> None:
        clean = (frozenset({"prepared", "start-permit", "launch", "heartbeat"}), None)
        failing = (frozenset({"prepared", "start-permit", "launch", "result"}), None)
        self.assertFalse(dispatch_blocked([clean]))
        self.assertTrue(dispatch_blocked([clean, failing]))


class CrashPrefixTest(unittest.TestCase):
    """§6.3: every prefix of every multi-file transition has one outcome, applied idempotently."""

    def test_every_prefix_has_exactly_one_outcome(self) -> None:
        for name, writes in TRANSITIONS.items():
            for length in range(len(writes) + 1):
                with self.subTest(transition=name, prefix=length):
                    self.assertIn((name, length), OUTCOMES)
                    self.assertIsInstance(recovery_outcome(name, length), str)

    def test_no_prefix_is_left_undefined_and_none_is_invented(self) -> None:
        expected = {
            (name, length)
            for name, writes in TRANSITIONS.items()
            for length in range(len(writes) + 1)
        }
        self.assertEqual(expected, set(OUTCOMES))

    def test_recovery_is_idempotent(self) -> None:
        for name, writes in TRANSITIONS.items():
            for length in range(len(writes) + 1):
                with self.subTest(transition=name, prefix=length):
                    once = apply_outcome(name, length)
                    twice = apply_outcome(name, once)
                    self.assertEqual(once, twice)
                    self.assertEqual(len(writes), once)

    def test_dispatch_has_exactly_one_ambiguous_prefix(self) -> None:
        """The permit-written-no-launch window is the only one resolved by evidence, not by rule."""
        ambiguous = [
            length for length in range(len(TRANSITIONS["dispatch"]) + 1)
            if "discover" in recovery_outcome("dispatch", length)
        ]
        self.assertEqual([2], ambiguous)

    def test_no_prefix_outcome_redispatches(self) -> None:
        """R3-07: no recovery outcome may resolve an ambiguous dispatch by launching again."""
        for (name, length), outcome in OUTCOMES.items():
            with self.subTest(transition=name, prefix=length):
                self.assertNotIn("redispatch", outcome)
                self.assertNotIn("launch again", outcome)


# --------------------------------------------------------------------------------------------------
# §11.2, §11.5, §11.6 — checks, captures, and which capture qualifies
# --------------------------------------------------------------------------------------------------

METHODS = ("command", "inspection", "review")
INDEPENDENCE = ("none", "separate_actor", "human")
OWNERS = ("worker", "coordinator")
RECORD_KINDS = {"command": "command_run", "inspection": "assessment", "review": "assessment"}
COMMAND_ONLY_FIELDS = ("argv", "cwd", "exit_status", "duration_ms", "streams")
ASSESSMENT_ONLY_FIELDS = ("criteria", "rationale", "assessor")


class Check:
    """One resolved check of a contract (§11.2)."""

    def __init__(self, check_id: str, method: str, independence: str, executed_by: str) -> None:
        self.check_id = check_id
        self.method = method
        self.independence = independence
        self.executed_by = executed_by


class Capture:
    """One capture record (§11.3). `fields` is the set of kind-specific field names present."""

    def __init__(
        self,
        check_id: str,
        record_kind: str,
        owner: str,
        sequence: int,
        verdict: str,
        relation: str,
        fields: "frozenset[str]" = frozenset(),
    ) -> None:
        self.check_id = check_id
        self.record_kind = record_kind
        self.owner = owner
        self.sequence = sequence
        self.verdict = verdict
        self.relation = relation
        self.fields = fields


def derivation_error(check: Check) -> bool:
    """§11.2: a producer cannot be independent of itself, so this contract must not be admitted."""
    return check.independence in ("separate_actor", "human") and check.executed_by == "worker"


def record_kind_ok(check: Check, capture: Capture) -> bool:
    return capture.record_kind == RECORD_KINDS[check.method]


def fields_ok(capture: Capture) -> bool:
    """§11.3: each kind requires its own fields and forbids the other kind's."""
    if capture.record_kind == "command_run":
        return not (capture.fields & frozenset(ASSESSMENT_ONLY_FIELDS))
    return not (capture.fields & frozenset(COMMAND_ONLY_FIELDS))


def independence_ok(check: Check, capture: Capture) -> bool:
    if check.independence == "none":
        return True
    if check.independence == "separate_actor":
        return capture.relation in ("separate", "human")
    return capture.relation == "human"


def considered(check: Check, captures: "tuple[Capture, ...]") -> "list[Capture]":
    """§11.5: captures in the running for this check, before precedence is applied.

    A worker capture for a `coordinator` check is *ignored*, not superseded: it was never asked for.
    """
    return [
        capture for capture in captures
        if capture.check_id == check.check_id
        and not (check.executed_by == "coordinator" and capture.owner == "worker")
    ]


def qualifying(check: Check, captures: "tuple[Capture, ...]") -> "Capture | None":
    pool = [
        capture for capture in considered(check, captures)
        if record_kind_ok(check, capture) and fields_ok(capture) and independence_ok(check, capture)
    ]
    if not pool:
        return None
    best_owner = "coordinator" if any(c.owner == "coordinator" for c in pool) else "worker"
    return max(
        (c for c in pool if c.owner == best_owner), key=lambda capture: capture.sequence
    )


def adequate(checks: "tuple[Check, ...]", captures: "tuple[Capture, ...]") -> bool:
    return all(qualifying(check, captures) is not None for check in checks)


def canonical_intent(checks: "tuple[Check, ...]", captures: "tuple[Capture, ...]") -> str:
    """§11.5: any failing qualifying capture makes the intent BLOCKED, whatever the worker said."""
    if not adequate(checks, captures):
        return "QUARANTINED"
    verdicts = [qualifying(check, captures) for check in checks]
    return "BLOCKED" if any(c is not None and c.verdict == "fail" for c in verdicts) else "DONE"


def worker_capture(check_id: str, kind: str, sequence: int = 1, verdict: str = "pass") -> Capture:
    return Capture(check_id, kind, "worker", sequence, verdict, "self")


def coordinator_capture(
    check_id: str, kind: str, sequence: int = 1, verdict: str = "pass", relation: str = "separate"
) -> Capture:
    return Capture(check_id, kind, "coordinator", sequence, verdict, relation)


class CaptureAdequacyTest(unittest.TestCase):
    """The invariants the fifth walkthrough scenario exposed, and problem 10's field exclusivity."""

    def test_independent_checks_must_be_coordinator_executed(self) -> None:
        for method in METHODS:
            for independence in INDEPENDENCE:
                for executed_by in OWNERS:
                    check = Check("c", method, independence, executed_by)
                    with self.subTest(method=method, independence=independence, by=executed_by):
                        self.assertEqual(
                            independence != "none" and executed_by == "worker",
                            derivation_error(check),
                        )

    def test_a_worker_capture_never_satisfies_a_coordinator_check(self) -> None:
        """The defect: an independent check has no worker capture *by design*."""
        check = Check("prose", "inspection", "separate_actor", "coordinator")
        self.assertEqual([], considered(check, (worker_capture("prose", "assessment"),)))
        self.assertIsNone(qualifying(check, (worker_capture("prose", "assessment"),)))
        self.assertIsNotNone(qualifying(check, (coordinator_capture("prose", "assessment"),)))

    def test_adequacy_before_the_coordinator_runs_its_checks_would_fail_every_plan(self) -> None:
        """§11.5's ordering rule, stated as the thing that goes wrong without it."""
        checks = (
            Check("pytest", "command", "none", "worker"),
            Check("prose", "inspection", "separate_actor", "coordinator"),
        )
        after_worker = (worker_capture("pytest", "command_run"),)
        self.assertFalse(adequate(checks, after_worker))
        after_coordinator = (*after_worker, coordinator_capture("prose", "assessment"))
        self.assertTrue(adequate(checks, after_coordinator))

    def test_a_review_check_is_adequate_only_with_a_human_assessment(self) -> None:
        check = Check("review", "review", "human", "coordinator")
        self.assertIsNone(qualifying(check, (coordinator_capture("review", "assessment"),)))
        human = coordinator_capture("review", "assessment", relation="human")
        self.assertIsNotNone(qualifying(check, (human,)))

    def test_a_capture_of_the_wrong_kind_never_qualifies(self) -> None:
        for method in METHODS:
            for kind in ("command_run", "assessment"):
                check = Check("c", method, "none", "worker")
                capture = worker_capture("c", kind)
                with self.subTest(method=method, kind=kind):
                    self.assertEqual(
                        kind == RECORD_KINDS[method], qualifying(check, (capture,)) is not None
                    )

    def test_no_capture_carries_both_kinds_of_fields(self) -> None:
        for kind, forbidden in (
            ("command_run", ASSESSMENT_ONLY_FIELDS),
            ("assessment", COMMAND_ONLY_FIELDS),
        ):
            for field in forbidden:
                capture = worker_capture("c", kind)
                capture.fields = frozenset({field})
                with self.subTest(kind=kind, field=field):
                    self.assertFalse(fields_ok(capture))

    def test_a_coordinator_rerun_supersedes_the_worker_capture(self) -> None:
        check = Check("pytest", "command", "none", "worker")
        failed = worker_capture("pytest", "command_run", sequence=1, verdict="fail")
        rerun = coordinator_capture("pytest", "command_run", sequence=1, verdict="pass")
        winner = qualifying(check, (failed, rerun))
        assert winner is not None
        self.assertEqual("coordinator", winner.owner)
        self.assertEqual("DONE", canonical_intent((check,), (failed, rerun)))
        self.assertEqual("BLOCKED", canonical_intent((check,), (failed,)))

    def test_the_highest_sequence_wins_within_one_owner(self) -> None:
        check = Check("pytest", "command", "none", "worker")
        captures = tuple(
            worker_capture("pytest", "command_run", sequence=n, verdict="fail" if n < 3 else "pass")
            for n in (1, 2, 3)
        )
        for order in (captures, tuple(reversed(captures))):
            with self.subTest(order=[c.sequence for c in order]):
                winner = qualifying(check, order)
                assert winner is not None
                self.assertEqual(3, winner.sequence)
                self.assertEqual("DONE", canonical_intent((check,), order))

    def test_multiple_commands_are_judged_separately(self) -> None:
        """Problem 10: several commands means several checks, so adequacy can name the failing one."""
        checks = (
            Check("pytest", "command", "none", "worker"),
            Check("ruff", "command", "none", "worker"),
        )
        both = (
            worker_capture("pytest", "command_run", verdict="pass"),
            worker_capture("ruff", "command_run", verdict="fail"),
        )
        self.assertTrue(adequate(checks, both))
        self.assertEqual("BLOCKED", canonical_intent(checks, both))
        self.assertEqual("QUARANTINED", canonical_intent(checks, both[:1]))

    def test_a_failing_verdict_outranks_a_worker_claiming_success(self) -> None:
        check = Check("pytest", "command", "none", "worker")
        failed = worker_capture("pytest", "command_run", verdict="fail")
        self.assertEqual("BLOCKED", canonical_intent((check,), (failed,)))

    def test_a_not_run_command_is_an_adequate_record_of_a_failure(self) -> None:
        """§11.6: a missing interpreter is a BLOCKED task, not a quarantined attempt."""
        check = Check("pytest", "command", "none", "coordinator")
        not_run = coordinator_capture("pytest", "command_run", verdict="fail")
        not_run.fields = frozenset({"argv", "cwd", "exit_status", "duration_ms", "streams"})
        self.assertTrue(adequate((check,), (not_run,)))
        self.assertEqual("BLOCKED", canonical_intent((check,), (not_run,)))


if __name__ == "__main__":
    unittest.main()
