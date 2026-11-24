"""Unit tests for the offline sync channel (slotcatalog.sync).

Run from the repository root:

    python3 -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from slotcatalog import Catalog
from slotcatalog.__main__ import main as cli_main
from slotcatalog.errors import (
    BatchError,
    CheckpointMismatch,
    EntryNotFound,
    InvalidOp,
    InvalidPackage,
    UnknownCheckpoint,
    WrongSource,
)
from slotcatalog.sync import Mirror, SyncSource, _digest, _make_package, validate_package


def reg(booklet, part, slot=None, **kw):
    op = {"op": "register", "booklet": booklet, "part": part}
    if slot is not None:
        op["slot"] = slot
    op.update(kw)
    return op


def make_state_doc():
    """A catalog state: B1 with approved A..D, all printed in V1."""
    cat = Catalog()
    cat.apply_batch(
        [
            {"op": "add_booklet", "booklet": "B1", "slot_width": 4},
            reg("B1", "A", title="旧件甲", body="甲的正文", approved=True),
            reg("B1", "B", title="旧件乙", body="乙的正文", approved=True),
            reg("B1", "C", title="现行件丙", body="丙的正文", approved=True),
            reg("B1", "D", title="件丁", body="丁的正文", approved=True),
            {"op": "publish", "version": "V1"},
        ],
        batch_id="SETUP",
    )
    return cat.save()


def make_source():
    return SyncSource.open(make_state_doc())


def chain_batch():
    return [
        {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
        {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
    ]


def pkg_rows(package):
    return package["rows"]


class TestSourceCheckpoints(unittest.TestCase):
    def test_open_checkpoint_is_zero_and_deterministic(self):
        src = make_source()
        self.assertEqual(src.checkpoint()["seq"], 0)
        again = SyncSource.open(make_state_doc())
        self.assertEqual(src.checkpoint(), again.checkpoint())

    def test_different_states_give_different_sources(self):
        src = make_source()
        other = Catalog()
        other.apply_batch(
            [{"op": "add_booklet", "booklet": "B9", "slot_width": 2}],
            batch_id="S",
        )
        src2 = SyncSource.open(other.save())
        self.assertNotEqual(src.source_id, src2.source_id)

    def test_successful_batch_advances_checkpoint_even_without_public_change(self):
        src = make_source()
        src.apply_batch(
            [{"op": "reserve", "booklet": "B1", "count": 3}], batch_id="RESERVE"
        )
        self.assertEqual(src.seq, 1)
        self.assertEqual(len(src.changelog), 1)
        changes = src.changelog[0]
        self.assertEqual(changes["docs"], {})
        self.assertEqual(changes["entries"], {})

    def test_failed_batch_does_not_advance_checkpoint(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        before = (src.seq, len(src.changelog), src.checkpoint())
        with self.assertRaises(BatchError):
            src.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "C", "new": "A"}],
                batch_id="CYCLE",
            )
        self.assertEqual((src.seq, len(src.changelog), src.checkpoint()),
                         (before[0], before[1], before[2]))

    def test_checkpoint_position_is_not_the_user_batch_id(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补"}],
            batch_id="CHAIN",  # same user name again: positions still differ
        )
        self.assertEqual(src.checkpoint()["seq"], 2)
        self.assertNotIn("CHAIN", json.dumps(src.checkpoint()))

    def test_restart_keeps_corrections_and_checkpoints_consistent(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补写"}],
            batch_id="AMEND",
        )
        src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": False},
             {"op": "replace", "booklet": "B1", "old": "B", "new": "D"}],
            batch_id="REDIRECT",
        )
        resumed = SyncSource.load(json.loads(json.dumps(src.save())))
        self.assertEqual(resumed.checkpoint(), src.checkpoint())
        for from_seq in range(src.seq + 1):
            self.assertEqual(
                resumed.delta_package(from_seq), src.delta_package(from_seq)
            )
        self.assertEqual(resumed.full_package(), src.full_package())
        # and the resumed source keeps accepting batches on the same timeline
        resumed.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": True}],
            batch_id="AFTER-RESTART",
        )
        self.assertEqual(resumed.seq, src.seq + 1)

    def test_delta_from_unknown_checkpoint_rejected(self):
        src = make_source()
        with self.assertRaises(UnknownCheckpoint):
            src.delta_package(1)
        with self.assertRaises(UnknownCheckpoint):
            src.delta_package(-1)
        with self.assertRaises(UnknownCheckpoint):
            src.delta_package(0, from_source="0" * 64)


class TestFullPackageAndFirstInstall(unittest.TestCase):
    def test_full_package_carries_both_tables_at_checkpoint(self):
        src = make_source()
        package = src.full_package()
        self.assertEqual(package["kind"], "full")
        self.assertIsNone(package["from"])
        self.assertEqual(package["to"], {"source": src.source_id, "seq": 0})
        docs = package["rows"]["docs"]["upsert"]
        self.assertEqual(sorted(docs["B1"]), ["A", "B", "C", "D"])
        entries = package["rows"]["entries"]["upsert"]
        self.assertEqual(
            entries["V1"]["B1"],
            {"0": {"booklet": "B1", "part": "A"},
             "1": {"booklet": "B1", "part": "B"},
             "2": {"booklet": "B1", "part": "C"},
             "3": {"booklet": "B1", "part": "D"}},
        )

    def test_first_install_then_views_match_source(self):
        src = make_source()
        mirror = Mirror()
        report = mirror.install(src.full_package())
        self.assertTrue(report["installed"])
        self.assertEqual(mirror.checkpoint(), src.checkpoint())
        self.assertEqual(mirror.export(), src.catalog.search_export())
        for slot in range(4):
            got = mirror.query("V1", "B1", slot)
            want = src.catalog.query_slot("V1", "B1", slot)
            self.assertEqual(got["endpoint"]["part"],
                             want["current"]["endpoint"]["part"])
            self.assertEqual(got["value"]["value"], want["current"]["value"])

    def test_first_package_must_be_full(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        mirror = Mirror()
        with self.assertRaises(CheckpointMismatch):
            mirror.install(src.delta_package(0))

    def test_mirror_roundtrip_is_byte_identical_and_resumable(self):
        src = make_source()
        mirror = Mirror()
        mirror.install(src.full_package())
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        text = json.dumps(mirror.save(), ensure_ascii=False, sort_keys=True)
        reloaded = Mirror.load(json.loads(text))
        text2 = json.dumps(reloaded.save(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(text, text2)
        # after a restart the mirror catches up from its own checkpoint
        report = reloaded.install(src.delta_package(0))
        self.assertTrue(report["installed"])
        self.assertEqual(reloaded.checkpoint(), src.checkpoint())
        self.assertEqual(reloaded.export(), src.catalog.search_export())


class TestDeltaNetting(unittest.TestCase):
    def test_changed_and_changed_back_rows_are_not_sent(self):
        src = make_source()
        stepwise = Mirror()
        stepwise.install(src.full_package())
        src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": False}],
            batch_id="FLIP",
        )
        stepwise.install(src.delta_package(stepwise.seq))
        src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": True}],
            batch_id="FLOP",
        )
        stepwise.install(src.delta_package(stepwise.seq))
        rows = pkg_rows(src.delta_package(0))
        self.assertEqual(rows, {"docs": {"upsert": {}, "delete": {}},
                                "entries": {"upsert": {}, "delete": {}}})
        # a mirror that watched every step ends where a jumper ends
        jumper = Mirror()
        jumper.install(src.full_package())
        self.assertEqual(
            json.dumps(stepwise.save(), ensure_ascii=False, sort_keys=True),
            json.dumps(jumper.save(), ensure_ascii=False, sort_keys=True),
        )

    def test_content_public_only_mid_window_is_not_smuggled_in(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [reg("B1", "E", body="乙的正文戊", approved=True)], batch_id="ADD-E"
        )
        src.apply_batch(
            [{"op": "withdraw", "booklet": "B1", "part": "E"}], batch_id="DROP-E"
        )
        package = src.delta_package(0)
        self.assertNotIn("戊", json.dumps(package, ensure_ascii=False))
        self.assertNotIn('"E"', json.dumps(package))

    def test_amend_only_updates_the_document_not_the_entries(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补写附件一"}],
            batch_id="AMEND",
        )
        rows = pkg_rows(src.delta_package(1))
        self.assertEqual(rows["entries"], {"upsert": {}, "delete": {}})
        self.assertEqual(rows["docs"]["delete"], {})
        self.assertEqual(
            rows["docs"]["upsert"]["B1"]["C"]["value"]["attachments"],
            ["补写附件一"],
        )

    def test_entry_to_non_public_endpoint_is_deleted_but_doc_kept(self):
        src = make_source()
        mirror = Mirror()
        mirror.install(src.full_package())
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": False},
             {"op": "replace", "booklet": "B1", "old": "B", "new": "D"}],
            batch_id="REDIRECT",
        )
        rows = pkg_rows(src.delta_package(1))
        # V1 slots of A and B used to point at C; their endpoint D is not
        # public now, so the entries die; D's own entry dies too
        self.assertEqual(rows["entries"]["delete"], {"V1": {"B1": [0, 1, 3]}})
        self.assertEqual(rows["entries"]["upsert"], {})
        # C is still a public endpoint in its own right: its document stays
        self.assertEqual(rows["docs"], {"upsert": {}, "delete": {"B1": ["D"]}})
        mirror.install(src.delta_package(mirror.seq))
        self.assertIn(("B1", "C"), mirror.docs)
        self.assertNotIn(("B1", "D"), mirror.docs)

    def test_empty_delta_still_moves_the_checkpoint(self):
        src = make_source()
        mirror = Mirror()
        mirror.install(src.full_package())
        src.apply_batch(
            [{"op": "reserve", "booklet": "B1", "count": 2}], batch_id="RESERVE"
        )
        package = src.delta_package(0)
        self.assertEqual(package["from"], {"source": src.source_id, "seq": 0})
        self.assertEqual(package["to"], {"source": src.source_id, "seq": 1})
        self.assertEqual(pkg_rows(package)["docs"]["upsert"], {})
        report = mirror.install(package)
        self.assertTrue(report["installed"])
        self.assertEqual(mirror.checkpoint(), {"source": src.source_id, "seq": 1})

    def test_deletes_carry_keys_only_upserts_carry_target_values(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        package = src.delta_package(0)
        rows = pkg_rows(package)
        self.assertEqual(rows["docs"]["delete"], {"B1": ["A", "B"]})
        for parts in rows["docs"]["delete"].values():
            for part in parts:
                self.assertIsInstance(part, str)
        upserts = rows["entries"]["upsert"]["V1"]["B1"]
        self.assertEqual(upserts["0"], {"booklet": "B1", "part": "C"})
        self.assertEqual(upserts["1"], {"booklet": "B1", "part": "C"})

    def test_two_mirrors_at_different_depths_converge(self):
        src = make_source()
        early_full = src.full_package()
        fast = Mirror()
        slow = Mirror()
        fast.install(early_full)
        slow.install(early_full)

        batches = [
            ("CHAIN", chain_batch()),                                            # 1
            ("AMEND", [{"op": "amend", "booklet": "B1", "part": "C",
                        "attachment": "补"}]),                                   # 2
            ("NOOP", [{"op": "reserve", "booklet": "B1", "count": 1}]),          # 3
            ("REDIRECT", [
                {"op": "set_review", "booklet": "B1", "part": "D",
                 "approved": False},
                {"op": "replace", "booklet": "B1", "old": "B", "new": "D"},
                reg("B1", "E", body="戊的正文", approved=True),
            ]),                                                                  # 4
            ("PUBLISH", [{"op": "publish", "version": "V2"}]),                   # 5
        ]
        # the fast mirror tracks every checkpoint as it appears; the slow
        # mirror misses batches 1..3 and only asks again at checkpoint 3
        for batch_id, ops in batches[:3]:
            src.apply_batch(ops, batch_id=batch_id)
            fast.install(src.delta_package(fast.seq))
        slow.install(src.delta_package(slow.seq))
        for batch_id, ops in batches[3:]:
            src.apply_batch(ops, batch_id=batch_id)
            fast.install(src.delta_package(fast.seq))
        # both now catch up to the source's current checkpoint from their own
        fast.install(src.delta_package(fast.seq))
        slow.install(src.delta_package(slow.seq))
        self.assertEqual(fast.checkpoint(), src.checkpoint())
        self.assertEqual(slow.checkpoint(), src.checkpoint())
        self.assertEqual(
            json.dumps(fast.save(), ensure_ascii=False, sort_keys=True),
            json.dumps(slow.save(), ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(slow.export(), src.catalog.search_export())


class TestMirrorInstallRejection(unittest.TestCase):
    def setUp(self):
        self.src = make_source()
        self.src.apply_batch(chain_batch(), batch_id="CHAIN")
        self.mirror = Mirror()
        self.mirror.install(self.src.full_package())

    def mirror_bytes(self):
        return json.dumps(self.mirror.save(), ensure_ascii=False, sort_keys=True)

    def test_retry_of_just_installed_package_is_a_no_op(self):
        self.src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补"}],
            batch_id="AMEND",
        )
        package = self.src.delta_package(1)
        first = self.mirror.install(package)
        self.assertTrue(first["installed"])
        before = self.mirror_bytes()
        second = self.mirror.install(package)
        self.assertFalse(second["installed"])
        self.assertEqual(second["reason"], "already_installed")
        self.assertEqual(self.mirror_bytes(), before)

    def test_delta_from_wrong_checkpoint_is_rejected(self):
        self.src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补"}],
            batch_id="AMEND",
        )
        self.src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补二"}],
            batch_id="AMEND2",
        )
        before = self.mirror_bytes()
        # the mirror is at seq 1; a delta starting anywhere else is rejected
        with self.assertRaises(CheckpointMismatch):
            self.mirror.install(self.src.delta_package(0))
        with self.assertRaises(CheckpointMismatch):
            self.mirror.install(self.src.delta_package(2))
        self.assertEqual(self.mirror_bytes(), before)

    def test_package_from_another_source_is_rejected(self):
        other = Catalog()
        other.apply_batch(
            [{"op": "add_booklet", "booklet": "BX", "slot_width": 2},
             reg("BX", "Q", body="q", approved=True)],
            batch_id="S",
        )
        src2 = SyncSource.open(other.save())
        before = self.mirror_bytes()
        with self.assertRaises(WrongSource):
            self.mirror.install(src2.full_package())
        self.assertEqual(self.mirror_bytes(), before)

    def test_stale_full_package_is_rejected(self):
        self.src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补"}],
            batch_id="AMEND",
        )
        self.mirror.install(self.src.delta_package(1))
        stale = _make_package(
            "full", None, {"source": self.src.source_id, "seq": 1},
            self.src.full_package()["rows"],
        )
        before = self.mirror_bytes()
        with self.assertRaises(CheckpointMismatch):
            self.mirror.install(stale)
        self.assertEqual(self.mirror_bytes(), before)

    def test_tampered_digest_is_rejected(self):
        package = self.src.delta_package(0)
        package["rows"]["docs"]["upsert"].setdefault("B1", {})["Z"] = {
            "booklet": "B1", "slot": 9, "slot_label": "0009", "part": "Z",
            "title": None, "value": {"body": "x", "attachments": []},
        }
        before = self.mirror_bytes()
        with self.assertRaises(InvalidPackage):
            self.mirror.install(package)
        self.assertEqual(self.mirror_bytes(), before)

    def test_truncated_and_shapeless_packages_are_rejected(self):
        before = self.mirror_bytes()
        for bad in (None, [], {"format": "slot-catalog-sync-package/1"},
                    {"format": "slot-catalog-sync-package/1", "kind": "full"}):
            with self.assertRaises(InvalidPackage):
                self.mirror.install(bad)
        self.assertEqual(self.mirror_bytes(), before)

    def test_dangling_entry_package_is_rejected(self):
        rows = {
            "docs": {"upsert": {}, "delete": {"B1": ["C"]}},
            "entries": {"upsert": {}, "delete": {}},
        }
        package = _make_package(
            "delta", {"source": self.src.source_id, "seq": 1},
            {"source": self.src.source_id, "seq": 2}, rows,
        )
        # entries V1/0 and V1/1 point at C; deleting C's doc would dangle them
        before = self.mirror_bytes()
        with self.assertRaises(InvalidPackage):
            self.mirror.install(package)
        self.assertEqual(self.mirror_bytes(), before)

    def test_full_package_can_fast_forward(self):
        self.src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "C", "attachment": "补"}],
            batch_id="AMEND",
        )
        report = self.mirror.install(self.src.full_package())
        self.assertTrue(report["installed"])
        self.assertEqual(self.mirror.checkpoint(), self.src.checkpoint())
        self.assertEqual(self.mirror.export(), self.src.catalog.search_export())


class TestMirrorReadViews(unittest.TestCase):
    def setUp(self):
        self.src = make_source()
        self.src.apply_batch(chain_batch(), batch_id="CHAIN")
        self.src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "D", "approved": False},
             {"op": "replace", "booklet": "B1", "old": "B", "new": "D"},
             reg("B1", "E", body="戊的正文", approved=True)],
            batch_id="REDIRECT",
        )
        self.src.apply_batch([{"op": "publish", "version": "V2"}], batch_id="PUB")
        self.mirror = Mirror()
        self.mirror.install(self.src.full_package())

    def test_historical_index_finds_current_public_item(self):
        got = self.mirror.query("V1", "B1", "0002")
        self.assertEqual(got["endpoint"], {"booklet": "B1", "part": "C"})
        self.assertEqual(got["value"]["value"]["body"], "丙的正文")
        got2 = self.mirror.query("V2", "B1", 4)
        self.assertEqual(got2["endpoint"]["part"], "E")

    def test_lapsed_entry_never_falls_back_to_cached_old_content(self):
        # V1 slot 0 (printed A) resolved to C before the redirect; C's
        # document is still cached, yet the lapsed entry must stay dead
        self.assertIn(("B1", "C"), self.mirror.docs)
        for slot in (0, 1, 3):
            with self.assertRaises(EntryNotFound):
                self.mirror.query("V1", "B1", slot)

    def test_unprinted_and_unknown_indexes_have_no_entry(self):
        with self.assertRaises(EntryNotFound):
            self.mirror.query("V1", "B1", 9)     # never printed
        with self.assertRaises(EntryNotFound):
            self.mirror.query("V9", "B1", 0)     # unknown version
        with self.assertRaises(EntryNotFound):
            self.mirror.query("V2", "B1", 3)     # printed but not approvable

    def test_entries_hold_keys_only_bodies_live_once(self):
        raw = json.dumps(self.mirror.save()["entries"], ensure_ascii=False)
        self.assertNotIn("正文", raw)
        c_docs = [k for k in self.mirror.docs if k == ("B1", "C")]
        self.assertEqual(len(c_docs), 1)
        pointed = [k for k, v in self.mirror.entries.items()
                   if v == {"booklet": "B1", "part": "C"}]
        self.assertEqual(sorted(pointed), [("V1", "B1", 2), ("V2", "B1", 2)])

    def test_export_matches_source_export(self):
        self.assertEqual(self.mirror.export(), self.src.catalog.search_export())
        self.assertEqual(
            self.mirror.export("B1"), self.src.catalog.search_export("B1")
        )


class TestIncrementalMaintenanceCost(unittest.TestCase):
    """The source must not re-walk unaffected history or unrelated parts."""

    def make_wide_source(self, versions=3, parts=40):
        cat = Catalog()
        ops = [{"op": "add_booklet", "booklet": "B1", "slot_width": 4}]
        for i in range(parts):
            ops.append(reg("B1", "P%03d" % i, body="b%d" % i, approved=True))
        ops.append({"op": "publish", "version": "V1"})
        cat.apply_batch(ops, batch_id="SETUP")
        for v in range(2, versions + 1):
            cat.apply_batch([{"op": "publish", "version": "V%d" % v}],
                            batch_id="PUB%d" % v)
        return SyncSource.open(cat.save())

    def test_amend_recomputes_only_its_own_rows(self):
        src = self.make_wide_source()
        src.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "P007", "attachment": "补"}],
            batch_id="AMEND",
        )
        self.assertEqual(src.stats["docs_recomputed"], 1)
        # P007 is printed in 3 versions -> exactly 3 entries reconsidered
        self.assertEqual(src.stats["entries_recomputed"], 3)

    def test_reserve_only_batch_recomputes_nothing(self):
        src = self.make_wide_source()
        src.apply_batch(
            [{"op": "reserve", "booklet": "B1", "count": 5}], batch_id="RES"
        )
        self.assertEqual(src.stats, {"docs_recomputed": 0, "entries_recomputed": 0})

    def test_edge_change_touches_only_the_affected_chain(self):
        src = self.make_wide_source()
        src.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "P001", "new": "P002"}],
            batch_id="EDGE",
        )
        # dirty docs: the two named parts; entries: only the two chains'
        # printed slots across the 3 versions
        self.assertEqual(src.stats["docs_recomputed"], 2)
        self.assertEqual(src.stats["entries_recomputed"], 2 * 3)

    def test_publish_indexes_the_new_edition_only(self):
        src = self.make_wide_source()
        src.apply_batch([{"op": "publish", "version": "V9"}], batch_id="PUB")
        self.assertEqual(src.stats["entries_recomputed"], 40)
        self.assertEqual(src.stats["docs_recomputed"], 0)


class TestMaintenanceEdgeCases(unittest.TestCase):
    def test_withdrawn_endpoint_kills_its_entries_and_doc(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "withdraw", "booklet": "B1", "part": "C"}], batch_id="W"
        )
        rows = pkg_rows(src.delta_package(1))
        self.assertEqual(rows["docs"]["delete"], {"B1": ["C"]})
        self.assertEqual(rows["entries"]["delete"], {"V1": {"B1": [0, 1, 2]}})
        self.assertEqual(rows["entries"]["upsert"], {})

    def test_unreplace_restores_the_part_as_its_own_endpoint(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "unreplace", "booklet": "B1", "old": "A"}], batch_id="UN"
        )
        rows = pkg_rows(src.delta_package(1))
        # A is a visible endpoint again: its doc is back and its printed
        # slot points at A once more
        self.assertEqual(rows["docs"]["upsert"]["B1"]["A"]["part"], "A")
        self.assertEqual(rows["entries"]["upsert"]["V1"]["B1"]["0"],
                         {"booklet": "B1", "part": "A"})
        self.assertEqual(rows["docs"]["delete"], {})

    def test_set_review_of_a_replaced_part_changes_nothing_public(self):
        src = make_source()
        src.apply_batch(chain_batch(), batch_id="CHAIN")
        src.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "A",
              "approved": False}],
            batch_id="REVIEW-REPLACED",
        )
        rows = pkg_rows(src.delta_package(1))
        self.assertEqual(rows, {"docs": {"upsert": {}, "delete": {}},
                                "entries": {"upsert": {}, "delete": {}}})

    def test_replace_and_publish_in_one_batch(self):
        src = make_source()
        src.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
             {"op": "publish", "version": "V2"}],
            batch_id="CHAIN-AND-PUBLISH",
        )
        # V2 records the post-batch graph: A is replaced and not printed
        self.assertNotIn(("V2", "B1", 0), src.entries)
        self.assertEqual(src.entries[("V2", "B1", 1)],
                         {"booklet": "B1", "part": "B"})
        self.assertEqual(src.entries[("V1", "B1", 0)],
                         {"booklet": "B1", "part": "B"})

    def test_publish_with_automatic_version_id(self):
        src = make_source()
        result = src.apply_batch([{"op": "publish"}], batch_id="AUTO")
        version_id = result["published"][0]
        self.assertEqual(version_id, "V2")
        self.assertEqual(src.entries[(version_id, "B1", 0)],
                         {"booklet": "B1", "part": "A"})

    def test_booklets_do_not_bleed_into_each_other(self):
        cat = Catalog()
        cat.apply_batch(
            [{"op": "add_booklet", "booklet": "B1", "slot_width": 2},
             {"op": "add_booklet", "booklet": "B2", "slot_width": 2},
             reg("B1", "A", body="a", approved=True),
             reg("B2", "A", body="other-a", approved=True),
             {"op": "publish", "version": "V1"}],
            batch_id="SETUP",
        )
        src = SyncSource.open(cat.save())
        src.apply_batch(
            [{"op": "withdraw", "booklet": "B2", "part": "A"}], batch_id="W"
        )
        rows = pkg_rows(src.delta_package(0))
        self.assertEqual(rows["docs"]["delete"], {"B2": ["A"]})
        self.assertEqual(rows["entries"]["delete"], {"V1": {"B2": [0]}})
        mirror = Mirror()
        mirror.install(src.full_package())
        self.assertEqual(
            mirror.query("V1", "B1", 0)["value"]["value"]["body"], "a")
        with self.assertRaises(EntryNotFound):
            mirror.query("V1", "B2", 0)


class TestPackageValidator(unittest.TestCase):
    def setUp(self):
        self.src = make_source()
        self.src.apply_batch(chain_batch(), batch_id="CHAIN")

    def test_valid_packages_round_trip(self):
        for package in (self.src.full_package(), self.src.delta_package(0)):
            normalized = validate_package(json.loads(json.dumps(package)))
            self.assertEqual(normalized["kind"], package["kind"])
            self.assertEqual(normalized["to"], package["to"])

    def test_backwards_or_cross_source_deltas_are_invalid(self):
        package = self.src.delta_package(0)
        package["from"] = {"source": self.src.source_id, "seq": 7}
        del package["sha256"]
        package["sha256"] = _digest(package)
        with self.assertRaises(InvalidPackage):
            validate_package(package)
        package = self.src.delta_package(0)
        package["from"]["source"] = "0" * 64
        del package["sha256"]
        package["sha256"] = _digest(package)
        with self.assertRaises(InvalidPackage):
            validate_package(package)

    def test_full_package_must_not_carry_a_from(self):
        package = self.src.full_package()
        package["from"] = {"source": self.src.source_id, "seq": 0}
        del package["sha256"]
        package["sha256"] = _digest(package)
        with self.assertRaises(InvalidPackage):
            validate_package(package)

    def test_row_shapes_are_checked(self):
        package = self.src.full_package()
        row = package["rows"]["docs"]["upsert"]["B1"]["C"]
        row["value"]["attachments"] = "not-a-list"
        del package["sha256"]
        package["sha256"] = _digest(package)
        with self.assertRaises(InvalidPackage):
            validate_package(package)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, doc):
        with open(self.path(name), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        return self.path(name)

    def run_cli(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(args)
        return code, buf.getvalue()

    def boot_source(self):
        self.run_cli(["init", "--out", self.path("s0.json")])
        self.write("b1.json", {"batch_id": "SETUP", "ops": [
            {"op": "add_booklet", "booklet": "B1", "slot_width": 4},
            reg("B1", "A", body="甲", approved=True),
            reg("B1", "B", body="乙", approved=True),
            {"op": "publish", "version": "V1"},
        ]})
        self.run_cli(["apply", self.path("s0.json"), self.path("b1.json"),
                      "--out", self.path("s1.json")])
        code, _ = self.run_cli(["sync-open", self.path("s1.json"),
                                "--out", self.path("src.json")])
        self.assertEqual(code, 0)

    def test_cli_full_flow(self):
        self.boot_source()
        code, out = self.run_cli(["sync-package", self.path("src.json"),
                                  "--out", self.path("pkg.json")])
        self.assertEqual(code, 0)
        code, out = self.run_cli(["mirror-install", self.path("pkg.json"),
                                  "--out", self.path("m.json")])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["installed"])
        self.assertEqual(report["docs"], 2)
        code, out = self.run_cli(["mirror-query", self.path("m.json"),
                                  "V1", "B1", "0001"])
        self.assertEqual(json.loads(out)["endpoint"]["part"], "B")
        code, out = self.run_cli(["mirror-export", self.path("m.json")])
        self.assertEqual(len(json.loads(out)["entries"]), 2)
        code, out = self.run_cli(["mirror-checkpoint", self.path("m.json")])
        self.assertEqual(json.loads(out)["checkpoint"]["seq"], 0)

    def test_cli_failed_batch_writes_nothing_and_keeps_checkpoint(self):
        self.boot_source()
        self.write("bad.json", {"batch_id": "X", "ops": [
            {"op": "replace", "booklet": "B1", "old": "A", "new": "A"}]})
        code, out = self.run_cli(["sync-apply", self.path("src.json"),
                                  self.path("bad.json"),
                                  "--out", self.path("src2.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "replacement_cycle")
        self.assertFalse(os.path.exists(self.path("src2.json")))
        code, out = self.run_cli(["sync-checkpoint", self.path("src.json")])
        self.assertEqual(json.loads(out)["checkpoint"]["seq"], 0)

    def test_cli_rejects_corrupt_and_foreign_packages(self):
        self.boot_source()
        self.run_cli(["sync-package", self.path("src.json"),
                      "--out", self.path("pkg.json")])
        self.run_cli(["mirror-install", self.path("pkg.json"),
                      "--out", self.path("m.json")])
        with open(self.path("pkg.json"), "rb") as fh:
            raw = fh.read()
        self.write("cut.json", {})  # placeholder, replaced by bytes below
        with open(self.path("cut.json"), "wb") as fh:
            fh.write(raw[: len(raw) // 2])
        code, out = self.run_cli(["mirror-install", self.path("cut.json"),
                                  "--mirror", self.path("m.json"),
                                  "--out", self.path("m2.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "invalid_package")
        self.assertFalse(os.path.exists(self.path("m2.json")))
        # a syntactically valid but tampered package
        doc = json.loads(raw)
        doc["to"]["seq"] = 5
        self.write("tampered.json", doc)
        code, out = self.run_cli(["mirror-install", self.path("tampered.json"),
                                  "--mirror", self.path("m.json"),
                                  "--out", self.path("m2.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "invalid_package")

    def test_cli_retry_is_noop_and_never_overwrites(self):
        self.boot_source()
        self.run_cli(["sync-package", self.path("src.json"),
                      "--out", self.path("pkg.json")])
        self.run_cli(["mirror-install", self.path("pkg.json"),
                      "--out", self.path("m.json")])
        code, out = self.run_cli(["mirror-install", self.path("pkg.json"),
                                  "--mirror", self.path("m.json"),
                                  "--out", self.path("m2.json")])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["installed"])
        with open(self.path("m.json"), "rb") as fh:
            first = fh.read()
        with open(self.path("m2.json"), "rb") as fh:
            self.assertEqual(first, fh.read())
        code, out = self.run_cli(["mirror-install", self.path("pkg.json"),
                                  "--mirror", self.path("m.json"),
                                  "--out", self.path("m2.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "output_exists")

    def test_cli_query_and_export_read_through_a_sync_source_file(self):
        self.boot_source()
        code, out = self.run_cli(["export", self.path("src.json")])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["entries"]), 2)
        code, out = self.run_cli(["query", self.path("src.json"), "V1", "B1", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["printed"]["part"], "A")


if __name__ == "__main__":
    unittest.main()
