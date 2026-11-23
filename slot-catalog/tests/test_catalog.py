"""Acceptance-grade unit tests for the stable-slot catalog.

Run from the repository root:

    python3 -m unittest discover -s tests -t . -v
"""

import json
import unittest

from slotcatalog import Catalog, run_scenario
from slotcatalog.errors import (
    AlreadyWithdrawn,
    BatchError,
    CapacityExceeded,
    DuplicateVersion,
    EdgeNotFound,
    PartExists,
    PartNotFound,
    SlotNotPrinted,
    SlotNotReserved,
    SlotOccupied,
    SlotOutOfRange,
    UnknownBooklet,
    UnknownVersion,
)


def reg(booklet, part, slot=None, **kw):
    op = {"op": "register", "booklet": booklet, "part": part}
    if slot is not None:
        op["slot"] = slot
    op.update(kw)
    return op


def make_chain_catalog():
    """Booklet B1 (width 4) with approved parts A..D printed in version V1."""
    cat = Catalog()
    cat.apply_batch(
        [
            {"op": "add_booklet", "booklet": "B1", "slot_width": 4},
            reg("B1", "A", title="Alpha", body="body-A", approved=True),
            reg("B1", "B", title="Beta", body="body-B", approved=True),
            reg("B1", "C", title="Gamma", body="body-C", approved=True),
            reg("B1", "D", title="Delta", body="body-D", approved=True),
            {"op": "publish", "version": "V1"},
        ],
        batch_id="SETUP",
    )
    slots = {p: cat.part_info("B1", p)["slot"] for p in "ABCD"}
    return cat, slots


class TestSlotsAndReservation(unittest.TestCase):
    def test_reserve_and_explicit_choice(self):
        cat = Catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B1", "slot_width": 2},
                {"op": "reserve", "booklet": "B1", "slots": [5, 6]},
                reg("B1", "P1"),  # implicit -> smallest fresh slot
                reg("B1", "P2", slot=6),  # explicit claim of a reserved slot
            ],
            batch_id="S1",
        )
        self.assertEqual(cat.part_info("B1", "P1")["slot"], 0)
        self.assertEqual(cat.part_info("B1", "P2")["slot"], 6)
        # implicit allocation never consumes the reserved pool
        cat.apply_batch([reg("B1", "P3")], batch_id="S2")
        self.assertEqual(cat.part_info("B1", "P3")["slot"], 1)

    def test_explicit_slot_must_be_reserved(self):
        cat = Catalog()
        cat.apply_batch(
            [{"op": "add_booklet", "booklet": "B1", "slot_width": 2}], batch_id="S1"
        )
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([reg("B1", "P1", slot=7)], batch_id="S2")
        self.assertEqual(ctx.exception.cause.code, "slot_not_reserved")

    def test_cannot_fill_used_slot_of_another_part(self):
        cat = Catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B1", "slot_width": 2},
                {"op": "reserve", "booklet": "B1", "slots": [6]},
                reg("B1", "P1", slot=6),
            ],
            batch_id="S1",
        )
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([reg("B1", "P2", slot=6)], batch_id="S2")
        self.assertEqual(ctx.exception.cause.code, "slot_occupied")

    def test_reserve_count_and_capacity(self):
        cat = Catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B1", "slot_width": 1},
                {"op": "reserve", "booklet": "B1", "count": 3},
            ],
            batch_id="S1",
        )
        cat.apply_batch([reg("B1", "P%d" % i) for i in range(7)], batch_id="S2")
        # booklet capacity is 10 ** 1 = 10 slots; 3 reserved + 7 used = 10
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([reg("B1", "overflow")], batch_id="S3")
        self.assertEqual(ctx.exception.cause.code, "capacity_exceeded")

    def test_slot_label_fixed_width(self):
        cat, slots = make_chain_catalog()
        self.assertEqual(cat.slot_label("B1", slots["A"]), "0000")
        self.assertEqual(cat.slot_label("B1", 7), "0007")
        # queries accept the printed zero-padded label as well as the integer
        r = cat.query_slot("V1", "B1", "%04d" % slots["A"])
        self.assertEqual(r["printed"]["part"], "A")
        self.assertEqual(r["slot_label"], "0000")


class TestLifecycle(unittest.TestCase):
    def test_amend_keeps_slot_and_appends(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "amend", "booklet": "B1", "part": "A", "attachment": "att-1"},
                {"op": "amend", "booklet": "B1", "part": "A", "attachment": "att-2"},
                {"op": "publish", "version": "V2"},
            ],
            batch_id="AMEND",
        )
        # 补写不变槽
        self.assertEqual(cat.part_info("B1", "A")["slot"], slots["A"])
        r = cat.query_slot("V1", "B1", slots["A"])
        self.assertEqual(r["printed"]["part"], "A")
        self.assertEqual(r["current"]["endpoint"]["part"], "A")
        self.assertEqual(r["current"]["status"], "public")
        self.assertEqual(r["current"]["value"]["attachments"], ["att-1", "att-2"])
        # the older paper version still resolves to the same (amended) part
        r2 = cat.query_slot("V2", "B1", slots["A"])
        self.assertEqual(r2["printed"]["part"], "A")

    def test_amend_requires_registered_part(self):
        cat, _ = make_chain_catalog()
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "amend", "booklet": "B1", "part": "GHOST", "attachment": "x"}],
                batch_id="BAD",
            )
        self.assertEqual(ctx.exception.cause.code, "part_not_found")

    def test_withdraw_makes_tombstone_and_keeps_slot(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "withdraw", "booklet": "B1", "part": "B"},
                {"op": "publish", "version": "V2"},
            ],
            batch_id="WD",
        )
        # the part number still owns its slot
        info = cat.part_info("B1", "B")
        self.assertEqual(info["slot"], slots["B"])
        self.assertTrue(info["withdrawn"])
        # old paper version still prints B; current status is withdrawn
        r = cat.query_slot("V1", "B1", slots["B"])
        self.assertEqual(r["printed"]["part"], "B")
        self.assertEqual(r["current"]["status"], "not_public")
        self.assertEqual(r["current"]["reason"], "withdrawn")
        # the new version no longer lists the withdrawn slot
        with self.assertRaises(SlotNotPrinted):
            cat.query_slot("V2", "B1", slots["B"])
        # the tombstone slot can never be reused
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([reg("B1", "NEW", slot=slots["B"])], batch_id="STEAL")
        self.assertEqual(ctx.exception.cause.code, "slot_occupied")
        # the part number cannot be registered twice
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([reg("B1", "B")], batch_id="REREG")
        self.assertEqual(ctx.exception.cause.code, "part_exists")
        # double withdrawal is rejected
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "withdraw", "booklet": "B1", "part": "B"}], batch_id="WD2"
            )
        self.assertEqual(ctx.exception.cause.code, "already_withdrawn")
        # withdrawn parts stay out of the search export
        export = cat.search_export("B1")
        self.assertNotIn("B", {e["part"] for e in export["entries"]})

    def test_review_flag_gates_publication(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "C", "approved": False}],
            batch_id="UNAPPROVE",
        )
        r = cat.query_slot("V1", "B1", slots["C"])
        self.assertEqual(r["current"]["status"], "not_public")
        self.assertEqual(r["current"]["reason"], "not_approved")
        export = cat.search_export("B1")
        self.assertNotIn("C", {e["part"] for e in export["entries"]})
        cat.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "C", "approved": True}],
            batch_id="APPROVE",
        )
        r = cat.query_slot("V1", "B1", slots["C"])
        self.assertEqual(r["current"]["status"], "public")


class TestReplacementChain(unittest.TestCase):
    def test_chain_abc_then_switch_to_invisible_d(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
                {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
            ],
            batch_id="EDGES-1",
        )
        # A and B both resolve to C and may reveal C while C is public
        for part in ("A", "B"):
            r = cat.query_slot("V1", "B1", slots[part])
            self.assertEqual(r["printed"]["part"], part)
            self.assertTrue(r["current"]["replaced"])
            self.assertEqual(r["current"]["endpoint"]["part"], "C")
            self.assertEqual(r["current"]["endpoint"]["slot"], slots["C"])
            self.assertEqual(r["current"]["status"], "public")
            self.assertEqual(r["current"]["value"]["body"], "body-C")
            # only the endpoint identity is reported, never the chain
            self.assertNotIn("chain", r["current"])

        # switch: B is now replaced by D, and D becomes invisible in the same batch
        cat.apply_batch(
            [
                {"op": "set_review", "booklet": "B1", "part": "D", "approved": False},
                {"op": "unreplace", "booklet": "B1", "old": "B"},
                {"op": "replace", "booklet": "B1", "old": "B", "new": "D"},
            ],
            batch_id="EDGES-2",
        )
        # A and B can no longer reveal C; they end at invisible D
        for part in ("A", "B"):
            r = cat.query_slot("V1", "B1", slots[part])
            self.assertEqual(r["current"]["endpoint"]["part"], "D")
            self.assertEqual(r["current"]["status"], "not_public")
            self.assertEqual(r["current"]["reason"], "not_approved")
            self.assertNotIn("value", r["current"])
            self.assertNotIn("body-C", json.dumps(r, ensure_ascii=False))

        # C is still a qualified endpoint and stands on its own stable slot
        r = cat.query_slot("V1", "B1", slots["C"])
        self.assertEqual(r["current"]["endpoint"]["part"], "C")
        self.assertEqual(r["current"]["status"], "public")
        self.assertEqual(r["current"]["value"]["body"], "body-C")
        export = cat.search_export("B1")
        by_part = {e["part"]: e for e in export["entries"]}
        self.assertIn("C", by_part)
        self.assertEqual(by_part["C"]["slot"], slots["C"])
        self.assertNotIn("A", by_part)  # replaced
        self.assertNotIn("B", by_part)  # replaced
        self.assertNotIn("D", by_part)  # not approved
        # every visible endpoint appears exactly once, marked with its own slot
        self.assertEqual(
            len(export["entries"]), len({e["slot"] for e in export["entries"]})
        )

    def test_replacer_keeps_its_own_stable_slot(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}],
            batch_id="E1",
        )
        export = cat.search_export("B1")
        by_part = {e["part"]: e for e in export["entries"]}
        self.assertEqual(by_part["B"]["slot"], slots["B"])
        self.assertNotIn("A", by_part)

    def test_replaced_part_exits_later_paper_versions(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
                {"op": "publish", "version": "V2"},
            ],
            batch_id="E1",
        )
        with self.assertRaises(SlotNotPrinted):
            cat.query_slot("V2", "B1", slots["A"])
        # ...but the old paper version still resolves A to its current endpoint
        r = cat.query_slot("V1", "B1", slots["A"])
        self.assertEqual(r["current"]["endpoint"]["part"], "B")

    def test_replace_overwrites_and_unreplace_removes(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}],
            batch_id="E1",
        )
        # same-batch drop-old + set-new, interpreted by the batch-final state
        cat.apply_batch(
            [
                {"op": "unreplace", "booklet": "B1", "old": "A"},
                {"op": "replace", "booklet": "B1", "old": "A", "new": "C"},
            ],
            batch_id="E2",
        )
        self.assertEqual(cat.part_info("B1", "A")["replaced_by"], "C")
        # plain overwrite convenience
        cat.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "D"}],
            batch_id="E3",
        )
        self.assertEqual(cat.part_info("B1", "A")["replaced_by"], "D")
        # unreplace without a replacement
        cat.apply_batch(
            [{"op": "unreplace", "booklet": "B1", "old": "A"}], batch_id="E4"
        )
        self.assertIsNone(cat.part_info("B1", "A")["replaced_by"])
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "unreplace", "booklet": "B1", "old": "A"}], batch_id="E5"
            )
        self.assertEqual(ctx.exception.cause.code, "edge_not_found")

    def test_cycle_batch_rejected_atomically(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}],
            batch_id="E1",
        )
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [
                    reg("B1", "X", body="x"),
                    {"op": "replace", "booklet": "B1", "old": "B", "new": "A"},
                ],
                batch_id="BAD",
            )
        self.assertEqual(ctx.exception.cause.code, "replacement_cycle")
        # the whole batch did not take effect: X was rolled back, A->B intact
        with self.assertRaises(PartNotFound):
            cat.part_info("B1", "X")
        self.assertEqual(cat.part_info("B1", "A")["replaced_by"], "B")
        self.assertIsNone(cat.part_info("B1", "B")["replaced_by"])
        # a self loop is a cycle too
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "C", "new": "C"}],
                batch_id="SELF",
            )
        self.assertEqual(ctx.exception.cause.code, "replacement_cycle")
        # three-party cycle A->B->C->A
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [
                    {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
                    {"op": "replace", "booklet": "B1", "old": "C", "new": "A"},
                ],
                batch_id="CYCLE3",
            )
        self.assertEqual(ctx.exception.cause.code, "replacement_cycle")

    def test_replacement_requires_occupied_slots(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [{"op": "reserve", "booklet": "B1", "slots": [9]}], batch_id="RSV"
        )
        # a reserved-but-empty slot has no part and cannot enter a chain
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "GHOST", "new": "A"}],
                batch_id="G1",
            )
        self.assertEqual(ctx.exception.cause.code, "part_not_found")
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "A", "new": "GHOST"}],
                batch_id="G2",
            )
        self.assertEqual(ctx.exception.cause.code, "part_not_found")

    def test_replacement_is_same_booklet_only(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B2", "slot_width": 3},
                reg("B2", "A", body="other booklet A", approved=True),
            ],
            batch_id="B2SETUP",
        )
        # part numbers are scoped per booklet: B2's "A" is a different part
        self.assertEqual(cat.part_info("B2", "A")["slot"], 0)
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "A", "new": "ZZZ"}],
                batch_id="X1",
            )
        self.assertEqual(ctx.exception.cause.code, "part_not_found")

    def test_withdrawn_endpoint_is_not_public_but_chain_still_resolves(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
                {"op": "withdraw", "booklet": "B1", "part": "B"},
            ],
            batch_id="E1",
        )
        r = cat.query_slot("V1", "B1", slots["A"])
        self.assertEqual(r["current"]["endpoint"]["part"], "B")
        self.assertEqual(r["current"]["status"], "not_public")
        self.assertEqual(r["current"]["reason"], "withdrawn")


class TestVersionsAndQueries(unittest.TestCase):
    def test_unknown_version(self):
        cat, slots = make_chain_catalog()
        with self.assertRaises(UnknownVersion):
            cat.query_slot("V9", "B1", slots["A"])

    def test_unknown_booklet(self):
        cat, slots = make_chain_catalog()
        with self.assertRaises(UnknownBooklet):
            cat.query_slot("V1", "NOPE", 0)
        with self.assertRaises(UnknownBooklet):
            cat.search_export("NOPE")

    def test_reserved_but_unprinted(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "reserve", "booklet": "B1", "slots": [9]},
                {"op": "publish", "version": "V2"},
            ],
            batch_id="RSV",
        )
        with self.assertRaises(SlotNotPrinted):
            cat.query_slot("V2", "B1", 9)

    def test_never_allocated_slot_not_printed(self):
        cat, _ = make_chain_catalog()
        with self.assertRaises(SlotNotPrinted):
            cat.query_slot("V1", "B1", 42)

    def test_slot_out_of_range(self):
        cat, _ = make_chain_catalog()
        with self.assertRaises(SlotOutOfRange):
            cat.query_slot("V1", "B1", 10000)
        with self.assertRaises(SlotOutOfRange):
            cat.query_slot("V1", "B1", -1)

    def test_duplicate_version_rejected(self):
        cat, _ = make_chain_catalog()
        with self.assertRaises(BatchError) as ctx:
            cat.apply_batch([{"op": "publish", "version": "V1"}], batch_id="DUP")
        self.assertEqual(ctx.exception.cause.code, "duplicate_version")

    def test_auto_version_ids(self):
        cat = Catalog()
        cat.apply_batch(
            [{"op": "add_booklet", "booklet": "B1", "slot_width": 2}], batch_id="S1"
        )
        self.assertEqual(cat.publish_version(), "V1")
        self.assertEqual(cat.publish_version(), "V2")

    def test_booklet_created_after_version_has_no_records_there(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "LATE", "slot_width": 2},
                reg("LATE", "P", approved=True),
            ],
            batch_id="LATE",
        )
        with self.assertRaises(SlotNotPrinted):
            cat.query_slot("V1", "LATE", 0)

    def test_query_response_shape(self):
        cat, slots = make_chain_catalog()
        r = cat.query_slot("V1", "B1", slots["A"])
        self.assertEqual(r["ok"], True)
        self.assertEqual(r["version"], "V1")
        self.assertEqual(r["booklet"], "B1")
        self.assertEqual(r["slot"], slots["A"])
        self.assertEqual(r["slot_label"], "%04d" % slots["A"])
        self.assertEqual(r["printed"], {"part": "A", "title": "Alpha"})
        self.assertEqual(
            r["current"]["endpoint"],
            {
                "part": "A",
                "slot": slots["A"],
                "slot_label": "%04d" % slots["A"],
                "title": "Alpha",
            },
        )
        self.assertEqual(r["current"]["status"], "public")
        self.assertEqual(
            r["current"]["value"], {"body": "body-A", "attachments": []}
        )


class TestSearchExport(unittest.TestCase):
    def test_export_unique_endpoints_with_stable_slots(self):
        cat, slots = make_chain_catalog()
        # fan-in: A and B both replaced by C; D unapproved
        cat.apply_batch(
            [
                {"op": "replace", "booklet": "B1", "old": "A", "new": "C"},
                {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
                {"op": "set_review", "booklet": "B1", "part": "D", "approved": False},
            ],
            batch_id="E1",
        )
        export = cat.search_export("B1")
        parts = [e["part"] for e in export["entries"]]
        self.assertEqual(parts, ["C"])  # exactly one qualified endpoint
        self.assertEqual(export["count"], 1)
        self.assertEqual(export["entries"][0]["slot"], slots["C"])
        self.assertEqual(export["entries"][0]["slot_label"], "%04d" % slots["C"])

    def test_export_all_booklets_sorted(self):
        cat, _ = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B2", "slot_width": 3},
                reg("B2", "P", body="p", approved=True),
            ],
            batch_id="B2",
        )
        export = cat.search_export()
        self.assertEqual(export["count"], 5)
        self.assertEqual(
            [(e["booklet"], e["slot"]) for e in export["entries"]],
            sorted((e["booklet"], e["slot"]) for e in export["entries"]),
        )


class TestLongSharedSuffix(unittest.TestCase):
    def test_thousands_of_entries_share_long_suffix(self):
        n_chain = 3000
        n_fan = 1000
        cat = Catalog()
        ops = [{"op": "add_booklet", "booklet": "BIG", "slot_width": 4}]
        ops += [
            reg("BIG", "p%d" % i, body="body-p%d" % i, approved=True)
            for i in range(n_chain)
        ]
        ops += [
            reg("BIG", "q%d" % i, body="body-q%d" % i, approved=True)
            for i in range(n_fan)
        ]
        ops.append(reg("BIG", "Z", body="body-Z", approved=True))
        cat.apply_batch(ops, batch_id="BUILD")
        cat.apply_batch([{"op": "publish", "version": "V1"}], batch_id="PUB")
        edges = [
            {"op": "replace", "booklet": "BIG", "old": "p%d" % i, "new": "p%d" % (i + 1)}
            for i in range(n_chain - 1)
        ]
        edges += [
            {"op": "replace", "booklet": "BIG", "old": "q%d" % i, "new": "p0"}
            for i in range(n_fan)
        ]
        cat.apply_batch(edges, batch_id="CHAIN")

        tail = "p%d" % (n_chain - 1)
        self.assertEqual(cat.resolve_endpoint("BIG", "p0"), tail)
        self.assertEqual(cat.resolve_endpoint("BIG", "q17"), tail)

        # memoized resolution stays linear even with thousands of entrances
        # sharing the same long suffix
        cat.follow_steps = 0
        for i in range(0, n_chain, 100):
            cat.resolve_endpoint("BIG", "p%d" % i)
        for i in range(n_fan):
            cat.resolve_endpoint("BIG", "q%d" % i)
        self.assertLessEqual(cat.follow_steps, 2 * (n_chain + n_fan))

        # one edge flip re-points every upstream entrance; history is untouched
        versions_before = json.dumps(cat.save()["versions"], sort_keys=True)
        cat.apply_batch(
            [{"op": "replace", "booklet": "BIG", "old": tail, "new": "Z"}],
            batch_id="FLIP",
        )
        self.assertEqual(cat.resolve_endpoint("BIG", "p0"), "Z")
        self.assertEqual(cat.resolve_endpoint("BIG", "q999"), "Z")
        versions_after = json.dumps(cat.save()["versions"], sort_keys=True)
        self.assertEqual(versions_before, versions_after)

        # cutting the chain re-points only the upstream side
        mid = n_chain // 2
        cat.apply_batch(
            [{"op": "unreplace", "booklet": "BIG", "old": "p%d" % mid}],
            batch_id="CUT",
        )
        self.assertEqual(cat.resolve_endpoint("BIG", "p0"), "p%d" % mid)
        self.assertEqual(cat.resolve_endpoint("BIG", "p%d" % (mid + 1)), "Z")

        # the visible catalog holds exactly the two qualified endpoints
        export = cat.search_export("BIG")
        self.assertEqual(
            sorted(e["part"] for e in export["entries"]), ["Z", "p%d" % mid]
        )
        # slot p0 was printed in V1 and still resolves through the live graph
        r = cat.query_slot("V1", "BIG", 0)
        self.assertEqual(r["printed"]["part"], "p0")
        self.assertEqual(r["current"]["endpoint"]["part"], "p%d" % mid)


class TestPersistence(unittest.TestCase):
    def test_state_roundtrip_is_byte_identical(self):
        cat, slots = make_chain_catalog()
        cat.apply_batch(
            [
                {"op": "amend", "booklet": "B1", "part": "A", "attachment": "att"},
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
                {"op": "withdraw", "booklet": "B1", "part": "D"},
                {"op": "publish", "version": "V2"},
            ],
            batch_id="MUT",
        )
        blob = json.dumps(cat.save(), ensure_ascii=False, sort_keys=True)
        cat2 = Catalog.load(json.loads(blob))
        blob2 = json.dumps(cat2.save(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(blob, blob2)
        # queries against the reloaded catalog match the original
        self.assertEqual(
            cat.query_slot("V1", "B1", slots["A"]),
            cat2.query_slot("V1", "B1", slots["A"]),
        )
        self.assertEqual(cat.search_export("B1"), cat2.search_export("B1"))
        # and the reloaded catalog keeps accepting local batches
        cat2.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "B", "new": "C"}],
            batch_id="AFTER-LOAD",
        )
        self.assertEqual(cat2.resolve_endpoint("B1", "A"), "C")


class TestScenarioRunner(unittest.TestCase):
    def test_scenario_end_to_end(self):
        scenario = {
            "format": "slot-catalog-scenario/1",
            "name": "mini",
            "steps": [
                {
                    "batch": {
                        "batch_id": "SETUP",
                        "ops": [
                            {"op": "add_booklet", "booklet": "B1", "slot_width": 3},
                            reg("B1", "A", body="a", approved=True),
                            reg("B1", "B", body="b", approved=True),
                            {"op": "publish", "version": "V1"},
                        ],
                    }
                },
                {
                    "batch": {
                        "batch_id": "E1",
                        "ops": [
                            {"op": "replace", "booklet": "B1", "old": "A", "new": "B"}
                        ],
                    }
                },
                {"query_slot": {"version": "V1", "booklet": "B1", "slot": 0}},
                {"query_slot": {"version": "VX", "booklet": "B1", "slot": 0}},
                {"search_export": {"booklet": "B1"}},
            ],
        }
        out = run_scenario(scenario)
        self.assertEqual(out["format"], "slot-catalog-results/1")
        self.assertEqual(len(out["results"]), 5)
        q = out["results"][2]["result"]
        self.assertEqual(q["current"]["endpoint"]["part"], "B")
        bad = out["results"][3]["result"]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "unknown_version")
        export = out["results"][4]["result"]
        self.assertEqual([e["part"] for e in export["entries"]], ["B"])
        # deterministic: running again yields the identical document
        self.assertEqual(
            json.dumps(out, sort_keys=True),
            json.dumps(run_scenario(scenario), sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
