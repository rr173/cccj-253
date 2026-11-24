"""Unit tests for the offline branch merge (slotcatalog.merge).

Run from the repository root:

    python3 -m unittest discover -s tests -t . -v
"""

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest

from slotcatalog import Catalog
from slotcatalog.__main__ import main as cli_main
from slotcatalog.errors import (
    BaseMismatch,
    BranchInvalid,
    CapacityExceeded,
    EdgeConflict,
    InvalidBranchFile,
    InvalidOp,
    PartConflict,
    ReplacementCycle,
    SlotConflict,
    UnsupportedOp,
)
from slotcatalog.merge import BRANCH_FORMAT, REPORT_FORMAT, merge_branches


def reg(booklet, part, slot=None, **kw):
    op = {"op": "register", "booklet": booklet, "part": part}
    if slot is not None:
        op["slot"] = slot
    op.update(kw)
    return op


def make_base():
    """Booklet B1 (width 2): parts A/B/C at slots 0/1/2, reservations 50/51,
    edge B->C, version V1, two logged batches."""
    cat = Catalog()
    cat.apply_batch(
        [
            {"op": "add_booklet", "booklet": "B1", "slot_width": 2},
            {"op": "reserve", "booklet": "B1", "slots": [50, 51]},
            reg("B1", "A", title="Alpha", body="body-A", approved=True),
            reg("B1", "B", title="Beta", body="body-B", approved=True),
            reg("B1", "C", title="Gamma", body="body-C"),
            {"op": "publish", "version": "V1"},
        ],
        batch_id="SETUP",
    )
    cat.apply_batch(
        [
            {"op": "amend", "booklet": "B1", "part": "A", "attachment": "base-att"},
            {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
        ],
        batch_id="BASE-2",
    )
    return cat.save()


def make_tiny_base():
    """Booklet T (width 1, capacity 10): parts p0..p4 at slots 0..4,
    reservation 5; only slots 6..9 are free."""
    cat = Catalog()
    cat.apply_batch(
        [
            {"op": "add_booklet", "booklet": "T", "slot_width": 1},
            {"op": "reserve", "booklet": "T", "slots": [5]},
        ]
        + [reg("T", "p%d" % i) for i in range(5)],
        batch_id="SETUP",
    )
    return cat.save()


def to_bytes(doc):
    return (
        json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MergeTestCase(unittest.TestCase):
    def setUp(self):
        self.base_doc = make_base()
        self.raw = to_bytes(self.base_doc)
        self.digest = sha(self.raw)

    def branch(self, name, batches, digest=None):
        return {
            "format": BRANCH_FORMAT,
            "branch": name,
            "base_sha256": digest or self.digest,
            "batches": batches,
        }

    def merge(self, *branches):
        return merge_branches(self.raw, list(branches))

    def noop(self, name):
        return self.branch(name, [])


class TestSlotMerging(MergeTestCase):
    def test_basic_merge_moves_and_report(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "batch_id": "A-1",
                    "ops": [
                        reg("B1", "X", body="body-X", approved=True),
                        {
                            "op": "amend",
                            "booklet": "B1",
                            "part": "A",
                            "attachment": "att-alpha",
                        },
                    ],
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "batch_id": "B-1",
                    "ops": [
                        reg("B1", "Y", body="body-Y", approved=True),
                        {
                            "op": "amend",
                            "booklet": "B1",
                            "part": "A",
                            "attachment": "att-beta",
                        },
                        {"op": "set_review", "booklet": "B1", "part": "C", "approved": True},
                    ],
                }
            ],
        )
        state, report = self.merge(alpha, beta)
        parts = state["booklets"]["B1"]["parts"]
        slots = state["booklets"]["B1"]["slots"]
        # both branches suggested slot 3; alpha (higher priority) stays, Y moves
        self.assertEqual(parts["X"]["slot"], 3)
        self.assertEqual(parts["Y"]["slot"], 4)
        self.assertEqual(slots["3"], {"state": "occupied", "part": "X"})
        self.assertEqual(slots["4"], {"state": "occupied", "part": "Y"})
        # amendments append in branch-priority order after the base ones
        self.assertEqual(
            parts["A"]["attachments"], ["base-att", "att-alpha", "att-beta"]
        )
        self.assertTrue(parts["C"]["approved"])
        # base parts and base reservations stay put
        self.assertEqual(parts["A"]["slot"], 0)
        self.assertEqual(slots["50"], {"state": "reserved", "part": None})
        self.assertEqual(slots["51"], {"state": "reserved", "part": None})
        # the report lists the relocation
        self.assertTrue(report["ok"])
        self.assertEqual(report["format"], REPORT_FORMAT)
        self.assertEqual(report["base_sha256"], self.digest)
        self.assertEqual(report["branches"], ["alpha", "beta"])
        self.assertEqual(
            report["moves"],
            [
                {
                    "branch": "beta",
                    "booklet": "B1",
                    "part": "Y",
                    "from_slot": 3,
                    "to_slot": 4,
                    "from_slot_label": "03",
                    "to_slot_label": "04",
                }
            ],
        )

    def test_highest_priority_stays_on_shared_suggestion(self):
        alpha = self.branch("alpha", [{"ops": [reg("B1", "X")]}])
        beta = self.branch("beta", [{"ops": [reg("B1", "Y")]}])
        state, report = self.merge(alpha, beta)
        parts = state["booklets"]["B1"]["parts"]
        self.assertEqual(parts["X"]["slot"], 3)
        self.assertEqual(parts["Y"]["slot"], 4)
        self.assertEqual([m["part"] for m in report["moves"]], ["Y"])
        # swapping the priority order swaps who keeps the slot
        state2, report2 = self.merge(beta, alpha)
        parts2 = state2["booklets"]["B1"]["parts"]
        self.assertEqual(parts2["Y"]["slot"], 3)
        self.assertEqual(parts2["X"]["slot"], 4)
        self.assertEqual([m["part"] for m in report2["moves"]], ["X"])

    def test_implicit_registration_hits_fixed_slot_moves(self):
        # alpha's empty explicit reservation fixes slot 3; beta's implicit
        # suggestion lands on it and must move
        alpha = self.branch(
            "alpha", [{"ops": [{"op": "reserve", "booklet": "B1", "slots": [3]}]}]
        )
        beta = self.branch("beta", [{"ops": [reg("B1", "Y")]}])
        state, report = self.merge(alpha, beta)
        booklet = state["booklets"]["B1"]
        self.assertEqual(booklet["slots"]["3"], {"state": "reserved", "part": None})
        self.assertEqual(booklet["parts"]["Y"]["slot"], 4)
        self.assertEqual(
            [(m["part"], m["from_slot"], m["to_slot"]) for m in report["moves"]],
            [("Y", 3, 4)],
        )

    def test_movers_take_smallest_free_slot_and_skip_reservations(self):
        alpha = self.branch("alpha", [{"ops": [reg("B1", "X1"), reg("B1", "X2")]}])
        beta = self.branch("beta", [{"ops": [reg("B1", "Y1"), reg("B1", "Y2")]}])
        state, report = self.merge(alpha, beta)
        parts = state["booklets"]["B1"]["parts"]
        slots = state["booklets"]["B1"]["slots"]
        self.assertEqual(parts["X1"]["slot"], 3)
        self.assertEqual(parts["X2"]["slot"], 4)
        # movers take the smallest remaining free slots, never the reserved pool
        self.assertEqual(parts["Y1"]["slot"], 5)
        self.assertEqual(parts["Y2"]["slot"], 6)
        self.assertEqual(slots["50"], {"state": "reserved", "part": None})
        self.assertEqual(slots["51"], {"state": "reserved", "part": None})
        self.assertEqual(
            [(m["part"], m["to_slot"]) for m in report["moves"]],
            [("Y1", 5), ("Y2", 6)],
        )

    def test_empty_reserves_merge_and_single_claim_occupies(self):
        alpha = self.branch(
            "alpha", [{"ops": [{"op": "reserve", "booklet": "B1", "slots": [10]}]}]
        )
        beta = self.branch(
            "beta", [{"ops": [{"op": "reserve", "booklet": "B1", "slots": [10]}]}]
        )
        state, _ = self.merge(alpha, beta)
        self.assertEqual(
            state["booklets"]["B1"]["slots"]["10"],
            {"state": "reserved", "part": None},
        )
        # one explicit claim on the shared reservation occupies it
        alpha2 = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "reserve", "booklet": "B1", "slots": [10]},
                        reg("B1", "X", slot=10),
                    ]
                }
            ],
        )
        state2, _ = self.merge(alpha2, beta)
        self.assertEqual(
            state2["booklets"]["B1"]["slots"]["10"],
            {"state": "occupied", "part": "X"},
        )

    def test_base_reservation_only_claimable_explicitly(self):
        alpha = self.branch("alpha", [{"ops": [reg("B1", "X")]}])
        beta = self.branch("beta", [{"ops": [reg("B1", "Y", slot=50)]}])
        state, _ = self.merge(alpha, beta)
        slots = state["booklets"]["B1"]["slots"]
        # the implicit registration never lands on a base reservation
        self.assertEqual(state["booklets"]["B1"]["parts"]["X"]["slot"], 3)
        # the explicit claim occupies the base reservation
        self.assertEqual(slots["50"], {"state": "occupied", "part": "Y"})
        self.assertEqual(slots["51"], {"state": "reserved", "part": None})

    def test_withdrawn_new_part_keeps_tombstone_slot(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        reg("B1", "X"),
                        {"op": "withdraw", "booklet": "B1", "part": "X"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        {"op": "reserve", "booklet": "B1", "slots": [10]},
                        reg("B1", "Y", slot=10),
                        {"op": "withdraw", "booklet": "B1", "part": "Y"},
                    ]
                }
            ],
        )
        state, _ = self.merge(alpha, beta)
        booklet = state["booklets"]["B1"]
        # implicit registration rule: tombstone at the suggested slot
        self.assertEqual(booklet["slots"]["3"], {"state": "tombstone", "part": "X"})
        self.assertTrue(booklet["parts"]["X"]["withdrawn"])
        # explicit registration rule: tombstone at the fixed slot
        self.assertEqual(booklet["slots"]["10"], {"state": "tombstone", "part": "Y"})
        self.assertTrue(booklet["parts"]["Y"]["withdrawn"])

    def test_capacity_exceeded_fails_whole_merge(self):
        raw = to_bytes(make_tiny_base())
        digest = sha(raw)

        def branch(name, prefix):
            return {
                "format": BRANCH_FORMAT,
                "branch": name,
                "base_sha256": digest,
                "batches": [
                    {"ops": [reg("T", "%s%d" % (prefix, i)) for i in range(4)]}
                ],
            }

        with self.assertRaises(CapacityExceeded) as ctx:
            merge_branches(raw, [branch("alpha", "q"), branch("beta", "r")])
        self.assertEqual(ctx.exception.code, "capacity_exceeded")
        self.assertEqual(ctx.exception.details["booklet"], "T")


class TestContentMerging(MergeTestCase):
    def test_amends_append_in_priority_order_keeping_duplicates(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "amend", "booklet": "B1", "part": "A", "attachment": "a1"},
                        {"op": "amend", "booklet": "B1", "part": "A", "attachment": "a2"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        {"op": "amend", "booklet": "B1", "part": "A", "attachment": "b1"},
                        {"op": "amend", "booklet": "B1", "part": "A", "attachment": "a1"},
                    ]
                }
            ],
        )
        state, _ = self.merge(alpha, beta)
        self.assertEqual(
            state["booklets"]["B1"]["parts"]["A"]["attachments"],
            ["base-att", "a1", "a2", "b1", "a1"],
        )

    def test_review_net_change_and_return_to_base_is_noop(self):
        # alpha flips A off and back on: no net change
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "set_review", "booklet": "B1", "part": "A", "approved": False},
                        {"op": "set_review", "booklet": "B1", "part": "A", "approved": True},
                    ]
                }
            ],
        )
        state, _ = self.merge(alpha, self.noop("beta"))
        self.assertTrue(state["booklets"]["B1"]["parts"]["A"]["approved"])
        # beta's net change applies even though alpha round-tripped
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        {"op": "set_review", "booklet": "B1", "part": "A", "approved": False}
                    ]
                }
            ],
        )
        state2, _ = self.merge(alpha, beta)
        self.assertFalse(state2["booklets"]["B1"]["parts"]["A"]["approved"])

    def test_withdraw_net_change_applies_once(self):
        op = {"op": "withdraw", "booklet": "B1", "part": "C"}
        alpha = self.branch("alpha", [{"ops": [dict(op)]}])
        beta = self.branch("beta", [{"ops": [dict(op)]}])
        state, _ = self.merge(alpha, beta)
        booklet = state["booklets"]["B1"]
        self.assertTrue(booklet["parts"]["C"]["withdrawn"])
        self.assertEqual(booklet["slots"]["2"], {"state": "tombstone", "part": "C"})

    def test_new_part_content_taken_from_own_branch(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        reg("B1", "X", body="body-X", attachments=["x0"]),
                        {"op": "amend", "booklet": "B1", "part": "X", "attachment": "x1"},
                        {"op": "set_review", "booklet": "B1", "part": "X", "approved": True},
                    ]
                }
            ],
        )
        state, _ = self.merge(alpha, self.noop("beta"))
        part = state["booklets"]["B1"]["parts"]["X"]
        self.assertEqual(part["attachments"], ["x0", "x1"])
        self.assertTrue(part["approved"])
        self.assertEqual(part["slot"], 3)


class TestEdgeMerging(MergeTestCase):
    def test_identical_edge_changes_merge_once(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "unreplace", "booklet": "B1", "old": "B"},
                        {"op": "replace", "booklet": "B1", "old": "B", "new": "A"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta", [{"ops": [{"op": "replace", "booklet": "B1", "old": "B", "new": "A"}]}]
        )
        state, _ = self.merge(alpha, beta)
        self.assertEqual(state["booklets"]["B1"]["edges"], {"B": "A"})

    def test_edge_delete_vs_retarget_conflicts(self):
        alpha = self.branch(
            "alpha", [{"ops": [{"op": "unreplace", "booklet": "B1", "old": "B"}]}]
        )
        beta = self.branch(
            "beta", [{"ops": [{"op": "replace", "booklet": "B1", "old": "B", "new": "A"}]}]
        )
        with self.assertRaises(EdgeConflict) as ctx:
            self.merge(alpha, beta)
        details = ctx.exception.details
        self.assertEqual(details["booklet"], "B1")
        self.assertEqual(details["part"], "B")
        self.assertEqual(
            details["changes"],
            [
                {"branch": "alpha", "target": None},
                {"branch": "beta", "target": "A"},
            ],
        )

    def test_edge_two_different_targets_conflicts(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        reg("B1", "X"),
                        {"op": "replace", "booklet": "B1", "old": "B", "new": "X"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        reg("B1", "Y"),
                        {"op": "replace", "booklet": "B1", "old": "B", "new": "Y"},
                    ]
                }
            ],
        )
        with self.assertRaises(EdgeConflict) as ctx:
            self.merge(alpha, beta)
        self.assertEqual(ctx.exception.details["part"], "B")

    def test_cycle_only_in_union_rejected_with_booklet_and_chain(self):
        # each branch is acyclic on its own; the union cycles A->B->A
        alpha = self.branch(
            "alpha", [{"ops": [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}]}]
        )
        beta = self.branch(
            "beta", [{"ops": [{"op": "replace", "booklet": "B1", "old": "B", "new": "A"}]}]
        )
        with self.assertRaises(ReplacementCycle) as ctx:
            self.merge(alpha, beta)
        details = ctx.exception.details
        self.assertEqual(details["booklet"], "B1")
        cycle = details["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"A", "B"})
        self.assertIn("B1", ctx.exception.message)

    def test_disjoint_edge_changes_merge(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        reg("B1", "X", approved=True),
                        {"op": "replace", "booklet": "B1", "old": "A", "new": "X"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        reg("B1", "Y", approved=True),
                        {"op": "replace", "booklet": "B1", "old": "B", "new": "Y"},
                    ]
                }
            ],
        )
        state, _ = self.merge(alpha, beta)
        self.assertEqual(
            state["booklets"]["B1"]["edges"], {"A": "X", "B": "Y"}
        )


class TestConflictDetection(MergeTestCase):
    def test_same_part_registered_by_two_branches_conflicts(self):
        # identical contents still conflict
        alpha = self.branch("alpha", [{"ops": [reg("B1", "X", body="same")]}])
        beta = self.branch("beta", [{"ops": [reg("B1", "X", body="same")]}])
        with self.assertRaises(PartConflict) as ctx:
            self.merge(alpha, beta)
        details = ctx.exception.details
        self.assertEqual(details["booklet"], "B1")
        self.assertEqual(details["part"], "X")
        self.assertEqual(details["branches"], ["alpha", "beta"])

    def test_explicit_slot_claim_conflict(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "reserve", "booklet": "B1", "slots": [10]},
                        reg("B1", "X", slot=10),
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        {"op": "reserve", "booklet": "B1", "slots": [10]},
                        reg("B1", "Y", slot=10),
                    ]
                }
            ],
        )
        with self.assertRaises(SlotConflict) as ctx:
            self.merge(alpha, beta)
        details = ctx.exception.details
        self.assertEqual(details["booklet"], "B1")
        self.assertEqual(details["slot"], 10)
        self.assertEqual(
            details["claims"],
            [{"branch": "alpha", "part": "X"}, {"branch": "beta", "part": "Y"}],
        )


class TestInputValidation(MergeTestCase):
    def test_base_mismatch(self):
        alpha = self.branch("alpha", [], digest="0" * 64)
        with self.assertRaises(BaseMismatch) as ctx:
            self.merge(alpha, self.noop("beta"))
        details = ctx.exception.details
        self.assertEqual(details["branch"], "alpha")
        self.assertEqual(details["expected"], "0" * 64)
        self.assertEqual(details["actual"], self.digest)

    def test_unsupported_ops(self):
        cases = [
            [{"op": "publish", "version": "V9"}],
            [{"op": "add_booklet", "booklet": "N", "slot_width": 1}],
            [{"op": "reserve", "booklet": "B1", "count": 2}],
        ]
        for ops in cases:
            alpha = self.branch("alpha", [{"batch_id": "BAD", "ops": ops}])
            with self.assertRaises(UnsupportedOp) as ctx:
                self.merge(alpha, self.noop("beta"))
            details = ctx.exception.details
            self.assertEqual(details["branch"], "alpha")
            self.assertEqual(details["batch_id"], "BAD")
            self.assertEqual(details["op_index"], 0)

    def test_branch_invalid_reports_batch_and_op(self):
        alpha = self.branch(
            "alpha", [{"batch_id": "BAD", "ops": [reg("B1", "A")]}]
        )
        with self.assertRaises(BranchInvalid) as ctx:
            self.merge(alpha, self.noop("beta"))
        details = ctx.exception.details
        self.assertEqual(details["branch"], "alpha")
        self.assertEqual(details["batch_id"], "BAD")
        self.assertEqual(details["op_index"], 0)
        self.assertEqual(details["cause"]["code"], "part_exists")

    def test_branch_cannot_borrow_parts_from_another_branch(self):
        alpha = self.branch("alpha", [{"ops": [reg("B1", "X")]}])
        beta = self.branch(
            "beta", [{"ops": [{"op": "replace", "booklet": "B1", "old": "A", "new": "X"}]}]
        )
        with self.assertRaises(BranchInvalid) as ctx:
            self.merge(alpha, beta)
        self.assertEqual(ctx.exception.details["cause"]["code"], "part_not_found")

    def test_duplicate_branch_names_rejected(self):
        with self.assertRaises(InvalidBranchFile):
            self.merge(self.noop("alpha"), self.noop("alpha"))

    def test_needs_at_least_two_branches(self):
        with self.assertRaises(InvalidOp):
            merge_branches(self.raw, [self.noop("alpha")])

    def test_bad_branch_file_rejected(self):
        with self.assertRaises(InvalidBranchFile):
            self.merge({"format": "nope"}, self.noop("beta"))
        bad_hash = self.noop("alpha")
        bad_hash["base_sha256"] = "not-hex"
        with self.assertRaises(InvalidBranchFile):
            self.merge(bad_hash, self.noop("beta"))
        no_batches = self.noop("alpha")
        del no_batches["batches"]
        with self.assertRaises(InvalidBranchFile):
            self.merge(no_batches, self.noop("beta"))


class TestMergedStateUsability(MergeTestCase):
    def test_versions_batches_counters_preserved(self):
        state, _ = self.merge(self.noop("alpha"), self.noop("beta"))
        self.assertEqual(state["versions"], self.base_doc["versions"])
        self.assertEqual(state["batches"], self.base_doc["batches"])
        self.assertEqual(state["counters"], self.base_doc["counters"])
        self.assertEqual(state["format"], "slot-catalog-state/1")

    def test_merged_state_is_loadable_and_usable(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        reg("B1", "X", body="body-X", approved=True),
                        {"op": "replace", "booklet": "B1", "old": "A", "new": "X"},
                    ]
                }
            ],
        )
        beta = self.branch("beta", [{"ops": [reg("B1", "Y", approved=True)]}])
        state, _ = self.merge(alpha, beta)
        cat = Catalog.load(state)
        # stable-slot query against the base paper version
        r = cat.query_slot("V1", "B1", 0)
        self.assertEqual(r["printed"]["part"], "A")
        self.assertEqual(r["current"]["endpoint"]["part"], "X")
        self.assertEqual(r["current"]["status"], "public")
        # search export sees the new endpoints
        parts = {e["part"] for e in cat.search_export("B1")["entries"]}
        self.assertIn("X", parts)
        self.assertIn("Y", parts)
        self.assertNotIn("A", parts)  # replaced
        # the merged state keeps accepting local batches
        cat.apply_batch([reg("B1", "Z")], batch_id="POST")
        self.assertEqual(cat.part_info("B1", "Z")["slot"], 5)
        # save/load roundtrip is byte-identical
        blob = json.dumps(cat.save(), ensure_ascii=False, sort_keys=True)
        cat2 = Catalog.load(json.loads(blob))
        self.assertEqual(
            blob, json.dumps(cat2.save(), ensure_ascii=False, sort_keys=True)
        )

    def test_merge_is_deterministic(self):
        alpha = self.branch(
            "alpha",
            [
                {
                    "ops": [
                        {"op": "reserve", "booklet": "B1", "slots": [10]},
                        reg("B1", "X1"),
                        reg("B1", "X2", slot=10),
                        {"op": "amend", "booklet": "B1", "part": "A", "attachment": "a"},
                    ]
                }
            ],
        )
        beta = self.branch(
            "beta",
            [
                {
                    "ops": [
                        reg("B1", "Y1"),
                        reg("B1", "Y2"),
                        {"op": "withdraw", "booklet": "B1", "part": "C"},
                    ]
                }
            ],
        )
        state1, report1 = self.merge(alpha, beta)
        state2, report2 = self.merge(alpha, beta)
        self.assertEqual(
            json.dumps(state1, sort_keys=True), json.dumps(state2, sort_keys=True)
        )
        self.assertEqual(
            json.dumps(report1, sort_keys=True), json.dumps(report2, sort_keys=True)
        )

    def test_booklets_are_independent(self):
        # the same part number in different booklets is not a conflict, and
        # slot suggestions are resolved per booklet
        cat = Catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B1", "slot_width": 2},
                {"op": "add_booklet", "booklet": "B2", "slot_width": 2},
                reg("B1", "A", approved=True),
                reg("B2", "A", approved=True),
            ],
            batch_id="SETUP",
        )
        raw = to_bytes(cat.save())
        digest = sha(raw)

        def branch(name, batches):
            return {
                "format": BRANCH_FORMAT,
                "branch": name,
                "base_sha256": digest,
                "batches": batches,
            }

        alpha = branch("alpha", [[reg("B1", "X"), reg("B2", "X")]])
        beta = branch("beta", [[reg("B1", "Y"), reg("B2", "Y")]])
        state, report = merge_branches(raw, [alpha, beta])
        b1 = state["booklets"]["B1"]["parts"]
        b2 = state["booklets"]["B2"]["parts"]
        self.assertEqual((b1["X"]["slot"], b1["Y"]["slot"]), (1, 2))
        self.assertEqual((b2["X"]["slot"], b2["Y"]["slot"]), (1, 2))
        self.assertEqual(
            {(m["booklet"], m["part"]) for m in report["moves"]},
            {("B1", "Y"), ("B2", "Y")},
        )

    def test_batch_as_bare_ops_list(self):
        alpha = self.branch("alpha", [[reg("B1", "X")]])
        state, _ = self.merge(alpha, self.noop("beta"))
        self.assertIn("X", state["booklets"]["B1"]["parts"])


class TestMergeCli(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_path = self._write("base.json", self.raw)
        self.alpha_path = self._write(
            "alpha.json",
            to_bytes(
                self.branch(
                    "alpha",
                    [
                        {
                            "batch_id": "A-1",
                            "ops": [
                                reg("B1", "X", body="body-X", approved=True),
                                {
                                    "op": "amend",
                                    "booklet": "B1",
                                    "part": "A",
                                    "attachment": "att-alpha",
                                },
                            ],
                        }
                    ],
                )
            ),
        )
        self.beta_path = self._write(
            "beta.json",
            to_bytes(
                self.branch(
                    "beta",
                    [
                        {
                            "batch_id": "B-1",
                            "ops": [reg("B1", "Y", body="body-Y", approved=True)],
                        }
                    ],
                )
            ),
        )
        self.out_path = os.path.join(self.tmp.name, "merged.json")

    def _write(self, name, raw):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def run_cli(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(args)
        return code, buf.getvalue()

    def test_cli_success_report_and_usable_state(self):
        code, out = self.run_cli(
            ["merge", self.base_path, self.alpha_path, self.beta_path,
             "--out", self.out_path]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertEqual(report["out"], self.out_path)
        self.assertEqual(len(report["moves"]), 1)
        self.assertEqual(report["moves"][0]["part"], "Y")
        # the written state is directly usable by query/export/apply
        code, out = self.run_cli(["query", self.out_path, "V1", "B1", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["printed"]["part"], "A")
        code, out = self.run_cli(["export", self.out_path, "--booklet", "B1"])
        self.assertEqual(code, 0)
        parts = {e["part"] for e in json.loads(out)["entries"]}
        # B is replaced by C (base edge) and C is not approved
        self.assertEqual(parts, {"A", "X", "Y"})
        batch_path = self._write(
            "post.json", to_bytes({"batch_id": "POST", "ops": [reg("B1", "Z")]})
        )
        out2 = os.path.join(self.tmp.name, "merged2.json")
        code, _ = self.run_cli(
            ["apply", self.out_path, batch_path, "--out", out2]
        )
        self.assertEqual(code, 0)
        with open(out2, encoding="utf-8") as fh:
            self.assertIn("Z", json.load(fh)["booklets"]["B1"]["parts"])

    def test_cli_refuses_to_overwrite_existing_output(self):
        with open(self.out_path, "w", encoding="utf-8") as fh:
            fh.write("pre-existing")
        code, out = self.run_cli(
            ["merge", self.base_path, self.alpha_path, self.beta_path,
             "--out", self.out_path]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "output_exists")
        with open(self.out_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "pre-existing")

    def test_cli_failure_writes_nothing_and_keeps_inputs(self):
        before = {
            path: sha(open(path, "rb").read())
            for path in (self.base_path, self.alpha_path, self.beta_path)
        }
        # alpha and beta both register X -> part conflict
        conflict_beta = self._write(
            "beta-conflict.json",
            to_bytes(self.branch("beta", [{"ops": [reg("B1", "X")]}])),
        )
        before[conflict_beta] = sha(open(conflict_beta, "rb").read())
        code, out = self.run_cli(
            ["merge", self.base_path, self.alpha_path, conflict_beta,
             "--out", self.out_path]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "part_conflict")
        self.assertFalse(os.path.exists(self.out_path))
        after = {path: sha(open(path, "rb").read()) for path in before}
        self.assertEqual(before, after)

    def test_cli_base_mismatch_exit_code(self):
        tampered = self._write(
            "tampered.json", to_bytes(self.branch("alpha", [], digest="f" * 64))
        )
        code, out = self.run_cli(
            ["merge", self.base_path, tampered, self.beta_path,
             "--out", self.out_path]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "base_mismatch")
        self.assertFalse(os.path.exists(self.out_path))

    def test_cli_usage_errors(self):
        code, _ = self.run_cli(["merge", self.base_path, self.alpha_path])
        self.assertEqual(code, 2)
        code, _ = self.run_cli(
            ["merge", self.base_path, self.alpha_path, self.beta_path]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
